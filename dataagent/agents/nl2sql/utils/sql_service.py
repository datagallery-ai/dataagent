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
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib.request import Request, urlopen

from dataagent.agents.nl2sql.errors import SQLServiceError
from dataagent.utils.constants import DEFAULT_NL2SQL_SQLITE_PROGRESS_INTERVAL, DEFAULT_NL2SQL_SQLITE_TIMEOUT


@dataclass
class PrestoConfig:
    host: str
    port: int
    user: str
    catalog: str
    schema: str

    def to_conn_kwargs(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class MySQLConfig:
    host: str
    port: int
    user: str
    password: str
    database: str

    def to_conn_kwargs(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class SQLiteConfig:
    path: str

    def to_conn_kwargs(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class UDNConfig:
    path: str
    explain_url: str | None = None

    def to_conn_kwargs(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class SparkConfig:
    warehouse_dir: str

    def to_conn_kwargs(self) -> dict[str, Any]:
        return self.__dict__.copy()


class SqlService(ABC):
    @abstractmethod
    def explain(self, sql: str) -> str | None:
        pass

    @abstractmethod
    def execute(self, sql: str) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
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


class PrestoService(BaseService):
    def __init__(self, config: PrestoConfig):
        super().__init__()
        self.config = config

    def _get_conn(self):
        if self._conn is None:
            import prestodb

            self._conn = prestodb.dbapi.connect(**self.config.to_conn_kwargs())
        return self._conn

    def _handle_explain_error(self, e: Exception) -> str:
        from prestodb.exceptions import PrestoUserError

        if isinstance(e, PrestoUserError):
            return str(e.message)
        raise e


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


class UDNService(SqlService):
    def __init__(self, config: UDNConfig):
        self.config = config

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def explain(self, sql: str) -> str | None:
        try:
            if self.config.explain_url:
                import requests

                response = requests.post(
                    self.config.explain_url,
                    params={
                        "auto_repair": "true",
                        "format_sql": "false",
                    },
                    data=sql.encode("utf-8"),
                    headers={
                        "Content-Type": "text/plain; charset=utf-8",
                    },
                    timeout=(10, 1800),
                )
                response.raise_for_status()
                error = response.json().get("error")
                return str(error) if error else None

            data = json.dumps({"sql": sql}).encode("utf-8")
            req = Request(self.config.path, data=data, headers={"Content-Type": "application/json"})
            with urlopen(req) as resp:
                result = json.loads(resp.read())
            if result.get("success"):
                return None
            return result.get("message", "Unknown error")
        except Exception as e:
            raise SQLServiceError(detail=str(e)) from e

    def execute(self, sql: str) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
        try:
            data = json.dumps({"sql": sql}).encode()
            req = Request(self.config.path, data=data, headers={"Content-Type": "application/json"})
            with urlopen(req) as resp:
                result = json.loads(resp.read())
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
        try:
            spark = self._get_spark()
            explain_df = spark.sql(f"EXPLAIN {sql}")
            explain_df.collect()
            return None
        except Exception as e:
            return str(e)

    def execute(self, sql: str) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
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


def build_sql_service(engine: str, config: dict[str, Any]) -> BaseService | UDNService:
    try:
        if engine == "presto":
            return PrestoService(PrestoConfig(**config))
        if engine == "mysql":
            return MySQLService(MySQLConfig(**config))
        if engine in {"sqlite", "sqlite3"}:
            return SQLiteService(SQLiteConfig(**config))
        if engine == "udn":
            return UDNService(UDNConfig(**config))
        if engine in {"hive", "spark"}:
            return SparkService(SparkConfig(**config))
    except Exception as e:
        raise SQLServiceError(detail=str(e)) from e
    raise SQLServiceError(detail=f"Unsupported database engine: {engine}")
