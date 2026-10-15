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
import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from nl2sql.constants import DEFAULT_NL2SQL_SQLITE_PROGRESS_INTERVAL, DEFAULT_NL2SQL_SQLITE_TIMEOUT
from nl2sql.errors import DataAgentError


def _sql_infra_error(exc: BaseException | str) -> DataAgentError:
    return DataAgentError(source="tool", fact=str(exc), component="nl2sql")


_ENGINE_DRIVERS = {"mysql": "pymysql", "gaussvector": "psycopg2", "hive": "pyspark", "spark": "pyspark"}
"""Optional third-party drivers per engine; sqlite uses the standard library."""


def check_engine_driver(engine: str) -> None:
    """Fail early when the optional driver for the configured engine is not installed."""
    package = _ENGINE_DRIVERS.get(engine)
    if package is None:
        return
    try:
        importlib.import_module(package)
    except ImportError as exc:
        raise DataAgentError(
            source="config",
            component="nl2sql",
            fact=f"数据库引擎 {engine} 需要可选依赖 {package}（未安装）",
        ) from exc


@dataclass
class MySQLConfig:
    host: str
    port: int
    user: str
    password: str
    database: str

    def to_conn_kwargs(self) -> dict[str, Any]:
        """Return connection kwargs for the SQL driver."""
        return self.__dict__.copy()


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
    def execute(
        self, sql: str, *, limit: int = -1
    ) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
        """Execute SQL and return result rows, fetching at most ``limit + 1`` rows.

        ``limit < 0`` fetches everything; ``limit >= 0`` bounds the fetch so the
        caller can detect truncation from the extra row.
        """
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
            raise _sql_infra_error(e) from e
        try:
            cursor.execute(f"EXPLAIN {sql}")
            cursor.fetchall()
            return None
        except Exception as e:
            try:
                return self._handle_explain_error(e)
            except Exception as exc:
                raise _sql_infra_error(exc) from exc

    def execute(
        self, sql: str, *, limit: int = -1
    ) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
        """Execute SQL using the underlying driver connection, bounded by ``limit``."""
        try:
            conn = self._get_conn()
            self._before_execute(conn)
            cursor = conn.cursor()
        except Exception as e:
            raise _sql_infra_error(e) from e
        try:
            cursor.execute(sql)
            if limit >= 0:
                rows = cursor.fetchmany(limit + 1)
            else:
                rows = cursor.fetchall()
            return [desc[0] for desc in cursor.description], rows, None
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


class MySQLService(BaseService):
    def __init__(self, config: MySQLConfig):
        super().__init__()
        self.config = config

    def _get_conn(self):
        if self._conn is None:
            import pymysql

            self._conn = pymysql.connect(charset="utf8mb4", **self.config.to_conn_kwargs())
        return self._conn

    def _handle_explain_error(self, e: Exception) -> str:
        return str(e)


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

    def execute(
        self, sql: str, *, limit: int = -1
    ) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
        """Execute SQL through the Spark session, bounded by ``limit``."""
        try:
            spark = self._get_spark()
            df = spark.sql(sql)
            columns = df.columns
            if limit >= 0:
                rows = [tuple(row) for row in df.take(limit + 1)]
            else:
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


def build_sql_service(engine: str, config: dict[str, Any]) -> SqlService:
    """Construct a SQL service implementation for the configured engine."""
    try:
        if engine == "mysql":
            return MySQLService(MySQLConfig(**config))
        if engine == "gaussvector":
            return GaussVectorService(GaussVectorConfig(**config))
        if engine in {"sqlite", "sqlite3"}:
            return SQLiteService(SQLiteConfig(**config))
        if engine in {"hive", "spark"}:
            return SparkService(SparkConfig(**config))
    except Exception as e:
        raise _sql_infra_error(e) from e
    raise _sql_infra_error(f"Unsupported database engine: {engine}")
