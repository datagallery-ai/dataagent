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
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from dataagent.agents.nl2sql.errors import SQLServiceError
from dataagent.utils.constants import DEFAULT_NL2SQL_SQLITE_PROGRESS_INTERVAL, DEFAULT_NL2SQL_SQLITE_TIMEOUT


def _strip_sql_comments_and_literals(sql: str) -> str:
    out: list[str] = []
    i = 0
    quote_char: str | None = None
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if quote_char:
            if ch == quote_char:
                if nxt == quote_char:
                    i += 2
                    continue
                quote_char = None
            i += 1
            continue
        if ch in {"'", '"', "`"}:
            quote_char = ch
            out.append(" ")
            i += 1
            continue
        if ch == "-" and nxt == "-":
            i = sql.find("\n", i + 2)
            if i == -1:
                break
            out.append(" ")
            continue
        if ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            if end == -1:
                break
            out.append(" ")
            i = end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _fallback_readonly_select_error(sql: str) -> str | None:
    cleaned = _strip_sql_comments_and_literals(sql)
    statements = [part.strip() for part in cleaned.split(";") if part.strip()]
    if len(statements) != 1:
        return "Only a single SQL statement is allowed."

    normalized = re.sub(r"\s+", " ", statements[0]).strip().lower()
    tokens = re.findall(r"\b[a-z_]+\b", normalized)
    if not tokens or tokens[0] not in {"select", "with"}:
        return "Only read-only SELECT statements are allowed."
    if "select" not in tokens or "into" in tokens:
        return "Only read-only SELECT statements are allowed."
    if re.search(
        r"\b(?:alter|attach|call|copy|create|delete|detach|drop|execute|grant|insert|load|merge|pragma|replace|"
        r"revoke|set|truncate|unload|update|use|vacuum)\b",
        normalized,
    ):
        return "Only read-only SELECT statements are allowed."
    return None


def _sqlglot_expr_classes(exp: Any, *names: str) -> tuple[type, ...]:
    return tuple(cls for cls in (getattr(exp, name, None) for name in names) if isinstance(cls, type))


def _readonly_select_sql_error(sql: str) -> str | None:
    if not isinstance(sql, str) or not sql.strip():
        return "SQL statement is required."
    if "\x00" in sql:
        return "Invalid SQL statement."

    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        return _fallback_readonly_select_error(sql)

    try:
        statements = sqlglot.parse(sql, error_level=sqlglot.errors.ErrorLevel.RAISE)
    except Exception:
        return "Invalid SQL statement."
    if len(statements) != 1:
        return "Only a single SQL statement is allowed."
    statement = statements[0]
    read_classes = _sqlglot_expr_classes(exp, "Select", "Union", "Except", "Intersect")
    write_classes = _sqlglot_expr_classes(
        exp,
        "Alter",
        "Create",
        "Delete",
        "Drop",
        "Insert",
        "Merge",
        "Truncate",
        "Update",
    )
    find = getattr(statement, "find", None)
    args = getattr(statement, "args", {})
    has_write = bool(write_classes and callable(find) and find(*write_classes))
    into_classes = _sqlglot_expr_classes(exp, "Into")
    has_into = bool(
        (into_classes and callable(find) and find(*into_classes)) or (isinstance(args, dict) and args.get("into"))
    )
    if not isinstance(statement, read_classes) or has_write or has_into:
        return "Only read-only SELECT statements are allowed."
    return None


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

    def to_conn_kwargs(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class SparkConfig:
    warehouse_dir: str

    def to_conn_kwargs(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _resolve_sqlite_path(path: str) -> Path:
    raw_path = str(path or "").strip()
    if not raw_path:
        raise ValueError("SQLite database path is required.")

    requested = Path(raw_path).expanduser()
    if requested.is_absolute():
        return requested.resolve()

    base_dir = Path.cwd().resolve()
    resolved = (base_dir / requested).resolve()
    try:
        resolved.relative_to(base_dir)
    except ValueError as exc:
        raise ValueError("SQLite database path must stay within the current working directory.") from exc
    return resolved


def _sqlite_readonly_uri(path: Path) -> str:
    return f"file:{quote(str(path), safe='/')}?mode=ro"


class SqlService(ABC):
    def explain(self, sql: str) -> str | None:
        error = _readonly_select_sql_error(sql)
        if error:
            return error
        return self._explain(sql)

    def execute(self, sql: str) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
        error = _readonly_select_sql_error(sql)
        if error:
            return None, None, error
        return self._execute(sql)

    @abstractmethod
    def _explain(self, sql: str) -> str | None:
        pass

    @abstractmethod
    def _execute(self, sql: str) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
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

    def _explain(self, sql: str) -> str | None:
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
        except Exception as e:
            raise SQLServiceError() from e
        try:
            cursor.execute(f"EXPLAIN {sql}")
            cursor.fetchall()
            return None
        except Exception as e:
            try:
                return self._handle_explain_error(e)
            except Exception as exc:
                raise SQLServiceError() from exc

    def _execute(self, sql: str) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
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
        db_path = _resolve_sqlite_path(self.config.path)
        self._conn = sqlite3.connect(_sqlite_readonly_uri(db_path), uri=True, check_same_thread=False)
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

    def _explain(self, sql: str) -> str | None:
        try:
            data = json.dumps({"sql": sql}).encode()
            req = Request(self.config.path, data=data, headers={"Content-Type": "application/json"})
            with urlopen(req) as resp:
                result = json.loads(resp.read())
            if result.get("success"):
                return None
            return result.get("message", "Unknown error")
        except Exception as e:
            raise SQLServiceError() from e

    def _execute(self, sql: str) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
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

    def _explain(self, sql: str) -> str | None:
        try:
            spark = self._get_spark()
            explain_df = spark.sql(f"EXPLAIN {sql}")
            explain_df.collect()
            return None
        except Exception:
            return "SQL explain failed."

    def _execute(self, sql: str) -> tuple[list[str] | None, list[tuple[Any, ...]] | None, str | None]:
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
    """Build a SQL service implementation for the configured database engine."""
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
