# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
import contextlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from dataagent.agents.nl2sql.errors import SQLServiceError
from dataagent.common_utils.outbound_tls import httpx_verify
from dataagent.utils.constants import DEFAULT_NL2SQL_SQLITE_PROGRESS_INTERVAL, DEFAULT_NL2SQL_SQLITE_TIMEOUT

_CLOUD_CORE_TIMEOUT = httpx.Timeout(connect=10.0, read=1800.0, write=1800.0, pool=10.0)


@dataclass
class GaussVectorConfig:
    host: str
    port: int
    user: str
    password: str
    database: str

    def to_conn_kwargs(self) -> dict[str, Any]:
        """Return connection kwargs for the SQL driver."""
        return self.__dict__.copy()


@dataclass
class SQLiteConfig:
    path: str

    def to_conn_kwargs(self) -> dict[str, Any]:
        """Return connection kwargs for the SQL driver."""
        return self.__dict__.copy()


@dataclass
class CloudCoreConfig:
    path: str
    explain_url: str | None = None

    def to_conn_kwargs(self) -> dict[str, Any]:
        """Return connection kwargs for the SQL driver."""
        return self.__dict__.copy()


@dataclass
class SparkConfig:
    warehouse_dir: str

    def to_conn_kwargs(self) -> dict[str, Any]:
        """Return connection kwargs for the SQL driver."""
        return self.__dict__.copy()


class SqlService(ABC):
    @abstractmethod
    def explain(self, sql: str) -> str | None:
        """Dry-run / explain SQL and return diagnostics."""
        pass

    @abstractmethod
    def execute(self, sql: str) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
        """Execute SQL and return result rows."""
        pass


class BaseService(SqlService, ABC):
    def __init__(self):
        self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self._conn:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None
        return False

    def explain(self, sql: str) -> str | None:
        """Explain SQL using the underlying driver connection."""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
        except Exception as e:
            raise SQLServiceError(detail=str(e)) from e
        try:
            cursor.execute(f"EXPLAIN {sql}")
            cursor.fetchall()
            return None
        except Exception as e:
            try:
                return self._handle_explain_error(e)
            except Exception as exc:
                raise SQLServiceError(detail=str(exc)) from exc

    def execute(self, sql: str) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
        """Execute SQL using the underlying driver connection."""
        try:
            conn = self._get_conn()
            self._before_execute(conn)
            cursor = conn.cursor()
        except Exception as e:
            raise SQLServiceError(detail=str(e)) from e
        try:
            cursor.execute(sql)
            return [desc[0] for desc in cursor.description], cursor.fetchall(), None
        except Exception as e:
            return None, None, str(e)

    def _before_execute(self, conn):
        pass

    @abstractmethod
    def _get_conn(self):
        pass

    @abstractmethod
    def _handle_explain_error(self, e: Exception) -> str:
        pass


class GaussVectorService(BaseService):
    def __init__(self, config: GaussVectorConfig):
        super().__init__()
        self.config = config

    def _get_conn(self):
        if self._conn is None:
            import psycopg2

            self._conn = psycopg2.connect(**self.config.to_conn_kwargs())
        return self._conn

    def _handle_explain_error(self, e: Exception) -> str:
        return str(e)


class SQLiteService(BaseService):
    TIME_OUT = DEFAULT_NL2SQL_SQLITE_TIMEOUT

    def __init__(self, config: SQLiteConfig):
        super().__init__()
        self.config = config

    def _get_conn(self):
        import sqlite3

        if self._conn:
            return self._conn
        self._conn = sqlite3.connect(f"file:{self.config.path}?mode=ro", uri=True, check_same_thread=False)
        return self._conn

    def _before_execute(self, conn):
        import time

        start_time = time.time()

        def progress_handler():
            if time.time() - start_time > self.TIME_OUT:
                return 1
            return 0

        conn.set_progress_handler(progress_handler, DEFAULT_NL2SQL_SQLITE_PROGRESS_INTERVAL)

    def _handle_explain_error(self, e: Exception) -> str:
        import sqlite3

        if isinstance(e, sqlite3.Error):
            if "interrupted" in str(e):
                return "Query timeout."
            return str(e)
        raise e


class CloudCoreService(SqlService):
    def __init__(self, config: CloudCoreConfig):
        self.config = config

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def explain(self, sql: str) -> str | None:
        """Explain SQL via the cloud-core HTTP endpoint."""
        try:
            verify = httpx_verify("cloud_core")
            if self.config.explain_url:
                response = httpx.post(
                    self.config.explain_url,
                    params={
                        "auto_repair": "true",
                        "format_sql": "false",
                    },
                    content=sql.encode("utf-8"),
                    headers={
                        "Content-Type": "text/plain; charset=utf-8",
                    },
                    timeout=_CLOUD_CORE_TIMEOUT,
                    verify=verify,
                )
                response.raise_for_status()
                error = response.json().get("error")
                return str(error) if error else None

            response = httpx.post(
                self.config.path,
                json={"sql": sql},
                timeout=_CLOUD_CORE_TIMEOUT,
                verify=verify,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("success"):
                return None
            return result.get("message", "Unknown error")
        except Exception as e:
            raise SQLServiceError(detail=str(e)) from e

    def execute(self, sql: str) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
        """Execute SQL against the cloud-core business-twin HTTP endpoint."""
        try:
            response = httpx.post(
                self.config.path,
                json={"sql": sql},
                timeout=_CLOUD_CORE_TIMEOUT,
                verify=httpx_verify("cloud_core"),
            )
            response.raise_for_status()
            result = response.json()
            if not result.get("success"):
                return None, None, result.get("message", "Unknown error")
            rows_data = result.get("data", [])
            if not rows_data:
                return [], [], None
            columns = list(rows_data[0].keys())
            rows = [tuple(row.get(col) for col in columns) for row in rows_data]
            return columns, rows, None
        except Exception as e:
            raise SQLServiceError(detail=str(e)) from e


class SparkService(SqlService):
    def __init__(self, config: SparkConfig):
        self.config = config
        self._spark = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self._spark:
            with contextlib.suppress(Exception):
                self._spark.stop()
            self._spark = None
        return False

    def explain(self, sql: str) -> str | None:
        """Explain SQL through the Spark session."""
        try:
            spark = self._get_spark()
            explain_df = spark.sql(f"EXPLAIN {sql}")
            explain_df.collect()
            return None
        except Exception as e:
            return str(e)

    def execute(self, sql: str) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
        """Execute SQL through the Spark session."""
        try:
            spark = self._get_spark()
            df = spark.sql(sql)
            columns = df.columns
            rows = [tuple(row) for row in df.collect()]
            return columns, rows, None
        except Exception as e:
            return None, None, str(e)

    def _get_spark(self):
        from pyspark.sql import SparkSession

        if self._spark is None:
            self._spark = (
                SparkSession.builder.config("spark.sql.warehouse.dir", self.config.warehouse_dir)
                .enableHiveSupport()
                .getOrCreate()
            )
        return self._spark


def build_sql_service(engine: str, config: dict[str, Any]) -> BaseService | CloudCoreService:
    """Construct a SQL service implementation for the given engine."""
    try:
        if engine == "gaussvector":
            return GaussVectorService(GaussVectorConfig(**config))
        if engine in {"sqlite", "sqlite3"}:
            return SQLiteService(SQLiteConfig(**config))
        if engine == "cloud_core":
            return CloudCoreService(CloudCoreConfig(**config))
        if engine in {"hive", "spark"}:
            return SparkService(SparkConfig(**config))
    except Exception as e:
        raise SQLServiceError(detail=str(e)) from e
    raise SQLServiceError(detail=f"Unsupported database engine: {engine}")
