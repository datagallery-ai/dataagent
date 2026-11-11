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
import json
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from mcp.shared.exceptions import McpError

from dataagent.core.errors import DataAgentError
from dataagent.utils.constants import DEFAULT_NL2SQL_SQLITE_PROGRESS_INTERVAL, DEFAULT_NL2SQL_SQLITE_TIMEOUT


def _sql_infra_error(exc: BaseException | str) -> DataAgentError:
    return DataAgentError(source="tool", fact=str(exc), component="nl2sql")


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
class CloudCoreConfig:
    path: str
    explain_url: str | None = None

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
        endpoint = urlsplit(self.path)
        if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
            raise ValueError("MCP path must be an HTTP or HTTPS endpoint URL")
        if not self.read_tool.strip():
            raise ValueError("MCP read_tool must not be empty")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("MCP timeout must be a positive finite number of seconds")
        self.explain_prefix = self.explain_prefix.strip()
        if not self.explain_prefix:
            raise ValueError("MCP explain_prefix must not be empty")


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

    def execute(self, sql: str) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
        """Execute SQL using the underlying driver connection."""
        try:
            conn = self._get_conn()
            self._before_execute(conn)
            cursor = conn.cursor()
        except Exception as e:
            raise _sql_infra_error(e) from e
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
            raise _sql_infra_error(e) from e

    def execute(self, sql: str) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
        """Execute SQL against the cloud-core business-twin HTTP endpoint."""
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
            raise _sql_infra_error(e) from e


class MCPSqlService(SqlService):
    """Adapt a remote MCP SQL read tool to the synchronous NL2SQL service API.

    NL2SQL runs these methods in worker threads. Each call owns its event loop
    and a short-lived MCP session; the shared client handles initialization and
    cleanup within the same async task, including cancellation on timeout.
    """

    def __init__(self, config: MCPConfig):
        self.config = config

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    async def _call_query_async(self, sql: str):
        from dataagent.actions.tools.mcp import MCPClientWrapper, MCPServerConfig

        client = MCPClientWrapper(
            MCPServerConfig(
                server_id="nl2sql",
                transport_type="streamable_http",
                config={
                    "url": self.config.path,
                    "timeout": self.config.timeout,
                },
            )
        )
        try:
            return await asyncio.wait_for(
                client.call_tool(self.config.read_tool, {"query": sql}, raise_tool_errors=True),
                timeout=self.config.timeout,
            )
        except TimeoutError as exc:
            raise TimeoutError(f"MCP SQL request timed out after {self.config.timeout} seconds") from exc

    def execute(self, sql: str) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
        try:
            response = asyncio.run(self._call_query_async(sql))
            texts = [item.text for item in response.content if getattr(item, "type", None) == "text"]
            if response.isError:
                return None, None, "\n".join(texts) or "MCP SQL tool failed"

            payload = getattr(response, "structuredContent", None)
            if payload is None:
                if len(texts) != 1:
                    raise ValueError("MCP SQL tool must return one JSON result or structuredContent")
                payload = json.loads(texts[0])
            if not isinstance(payload, dict):
                raise ValueError("MCP SQL result must be an object")
            if payload.get("success") is False or payload.get("error"):
                return None, None, str(payload.get("error") or payload.get("message") or "MCP SQL query failed")

            columns, rows = payload.get("columns"), payload.get("rows")
            if not isinstance(columns, list) or not all(isinstance(column, str) for column in columns):
                raise ValueError("MCP SQL result must contain a columns list of strings")
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError("MCP SQL result must contain a rows list of objects")
            return columns, [tuple(row.get(column) for column in columns) for row in rows], None
        except McpError as exc:
            return None, None, exc.error.message or str(exc)
        except Exception as exc:
            raise _sql_infra_error(exc) from exc

    def explain(self, sql: str) -> str | None:
        """Submit EXPLAIN without falling back to executing the original query."""
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


def build_sql_service(engine: str, config: dict[str, Any]) -> SqlService:
    """Construct a SQL service implementation for the given engine."""
    try:
        if engine == "mysql":
            return MySQLService(MySQLConfig(**config))
        if engine == "gaussvector":
            return GaussVectorService(GaussVectorConfig(**config))
        if engine in {"sqlite", "sqlite3"}:
            return SQLiteService(SQLiteConfig(**config))
        if engine == "cloud_core":
            return CloudCoreService(CloudCoreConfig(**config))
        if engine == "mcp":
            return MCPSqlService(MCPConfig(**config))
        if engine in {"hive", "spark"}:
            return SparkService(SparkConfig(**config))
    except Exception as e:
        raise _sql_infra_error(e) from e
    raise _sql_infra_error(f"Unsupported database engine: {engine}")
