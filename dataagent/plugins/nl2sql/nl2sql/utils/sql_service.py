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
import asyncio
import contextlib
import importlib
import json
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import mcp
from mcp.client import streamable_http
from mcp.shared.exceptions import McpError

from nl2sql.constants import DEFAULT_NL2SQL_SQLITE_PROGRESS_INTERVAL, DEFAULT_NL2SQL_SQLITE_TIMEOUT
from nl2sql.errors import DataAgentError


def _sql_infra_error(exc: BaseException | str) -> DataAgentError:
    return DataAgentError(source="tool", fact=str(exc), component="nl2sql")


_ENGINE_DRIVERS = {
    "mysql": ("pymysql", None),
    "gaussvector": ("psycopg2", None),
    "hive": ("pyspark", "spark"),
    "spark": ("pyspark", "spark"),
}
"""Driver modules and optional extras per engine; sqlite uses the standard library."""


def check_engine_driver(engine: str) -> None:
    """Fail early when the driver for the configured engine is not installed."""
    driver = _ENGINE_DRIVERS.get(engine)
    if driver is None:
        return
    package, extra = driver
    try:
        importlib.import_module(package)
    except ImportError as exc:
        dependency = "可选依赖" if extra else "基础运行依赖"
        requirement = f"datagallery-dataagent[{extra}]" if extra else "datagallery-dataagent"
        raise DataAgentError(
            source="config",
            component="nl2sql",
            fact=f"数据库引擎 {engine} 缺少{dependency} {package}；请在运行环境安装 {requirement}",
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
class MCPConfig:
    """Streamable HTTP endpoint exposing a SQL read tool."""

    path: str
    read_tool: str = "dme_read_query"
    timeout: float = 180.0
    explain_prefix: str = "EXPLAIN PLAN FOR"

    def __post_init__(self):
        if not isinstance(self.path, str):
            raise ValueError("database.path must be an HTTP or HTTPS endpoint URL")
        endpoint = urlsplit(self.path)
        if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
            raise ValueError("database.path must be an HTTP or HTTPS endpoint URL")
        if not isinstance(self.read_tool, str) or not self.read_tool.strip():
            raise ValueError("database.read_tool must be a non-empty string")
        if (
            not isinstance(self.timeout, (int, float))
            or isinstance(self.timeout, bool)
            or not 0 < self.timeout <= sys.float_info.max
        ):
            raise ValueError("database.timeout must be a positive finite number of seconds")
        if not isinstance(self.explain_prefix, str) or not self.explain_prefix.strip():
            raise ValueError("database.explain_prefix must be a non-empty string")
        self.explain_prefix = self.explain_prefix.strip()


class MCPInfrastructureError(DataAgentError):
    """An MCP connection or result-contract failure that SQL reflection cannot fix."""

    def __init__(self, exc: BaseException | str):
        while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
            exc = exc.exceptions[0]
        super().__init__(source="tool", fact=str(exc), component="nl2sql")


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
        self._conn = sqlite3.connect(
            f"file:{self.config.path}?mode=ro", uri=True, check_same_thread=False
        )
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


class MCPSqlService(SqlService):
    """SQL service called in worker threads, with one MCP session per query."""

    def __init__(self, config: MCPConfig):
        self.config = config

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    async def _call_query_async(self, sql: str):
        try:
            async with asyncio.timeout(self.config.timeout):
                async with httpx.AsyncClient(timeout=self.config.timeout) as http_client:
                    async with streamable_http.streamable_http_client(
                        self.config.path, http_client=http_client
                    ) as (read, write, _):
                        async with mcp.ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            while not any(
                                tool.name == self.config.read_tool for tool in tools.tools
                            ):
                                if not tools.nextCursor:
                                    raise MCPInfrastructureError(
                                        f"MCP SQL read tool not found: {self.config.read_tool}"
                                    )
                                tools = await session.list_tools(
                                    params=mcp.types.PaginatedRequestParams(cursor=tools.nextCursor)
                                )
                            try:
                                return await session.call_tool(
                                    self.config.read_tool, {"query": sql}
                                )
                            except McpError as exc:
                                if exc.error.code in {
                                    httpx.codes.REQUEST_TIMEOUT,
                                    mcp.types.CONNECTION_CLOSED,
                                } or (
                                    exc.error.code == 32600
                                    and exc.error.message == "Session terminated"
                                ):
                                    raise
                                return exc
        except TimeoutError as exc:
            raise MCPInfrastructureError(
                f"MCP SQL request timed out after {self.config.timeout} seconds"
            ) from exc
        except Exception as exc:
            raise MCPInfrastructureError(exc) from exc

    def execute(
        self, sql: str, *, limit: int = -1
    ) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
        try:
            response = asyncio.run(self._call_query_async(sql))
            if isinstance(response, McpError):
                return None, None, response.error.message or str(response)
            texts = [
                item.text for item in response.content if getattr(item, "type", None) == "text"
            ]
            if response.isError:
                return None, None, "\n".join(texts) or "MCP SQL tool failed"
            payload = getattr(response, "structuredContent", None)
            if payload is None:
                if len(texts) != 1:
                    raise ValueError(
                        "MCP SQL tool must return one JSON result or structuredContent"
                    )
                payload = json.loads(texts[0])
            if not isinstance(payload, dict):
                raise ValueError("MCP SQL result must be an object")
            if payload.get("success") is False or payload.get("error"):
                return (
                    None,
                    None,
                    str(payload.get("error") or payload.get("message") or "MCP SQL query failed"),
                )
            columns, rows = payload.get("columns"), payload.get("rows")
            if not isinstance(columns, list) or not all(
                isinstance(column, str) for column in columns
            ):
                raise ValueError("MCP SQL result must contain a columns list of strings")
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError("MCP SQL result must contain a rows list of objects")
            # The remote tool has no fetch-limit argument; retain one extra row for truncation.
            rows = rows[: limit + 1] if limit >= 0 else rows
            try:
                values = [tuple(row[column] for column in columns) for row in rows]
            except KeyError as exc:
                raise ValueError(f"MCP SQL row missing column: {exc.args[0]!r}") from exc
            return columns, values, None
        except MCPInfrastructureError:
            raise
        except Exception as exc:
            raise MCPInfrastructureError(exc) from exc

    def explain(self, sql: str) -> str | None:
        """Submit EXPLAIN through the same read tool, without executing a fallback query."""
        _, _, error = self.execute(f"{self.config.explain_prefix} {sql}")
        return error


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
    if engine == "mcp":
        try:
            return MCPSqlService(MCPConfig(**config))
        except Exception as exc:
            raise MCPInfrastructureError(exc) from exc
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
