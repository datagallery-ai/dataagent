"""Database service factory and SQLite metadata and read-only operations."""

import contextlib
import csv
import io
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._sql_drivers import (
    DEFAULT_SQLITE_PROGRESS_INTERVAL,
    GaussVectorConfig,
    GaussVectorService,
    MCPConfig,
    MCPInfrastructureError,
    MySQLConfig,
    MySQLService,
    SparkConfig,
    SparkService,
    SqlService,
)
from ._sql_drivers import (
    SQLiteConfig as SQLiteDriverConfig,
)
from ._sql_drivers import (
    SQLiteService as SQLiteDriver,
)


@dataclass
class SQLiteConfig(SQLiteDriverConfig):
    descriptions_dir: str | None = None


class UnknownTableError(ValueError):
    code = "unknown_table"


class SQLiteService(SQLiteDriver):
    def __init__(
        self,
        config: SQLiteConfig,
        *,
        timeout_seconds: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ):
        super().__init__(config)
        if not isinstance(config.path, str) or not config.path.strip():
            raise ValueError("database.path is required for SQLite")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        # An explicit budget covers the whole service lifetime, including all
        # queries in describe_table. Legacy callers retain their per-query timeout.
        self._deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        self._cancelled = cancelled or (lambda: False)

    def _remaining_time(self) -> float:
        if self._cancelled():
            raise TimeoutError("SQLite execution deadline exceeded or cancelled")
        if self._deadline is None:
            # The per-query limit is enforced by SQLite's progress callback.
            return self.TIME_OUT
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("SQLite execution deadline exceeded or cancelled")
        return remaining

    def _get_conn(self):
        if self._conn is not None:
            return self._conn
        remaining = self._remaining_time()
        conn = super()._get_conn()
        conn.execute(f"PRAGMA busy_timeout = {int(min(5.0, remaining) * 1000)}")
        return conn

    def _connection_path(self) -> str:
        return Path(self.config.path).expanduser().resolve().as_uri().removeprefix("file:")

    def _before_execute(self, conn):
        deadline = time.monotonic() + self._remaining_time()
        conn.set_progress_handler(
            lambda: int(self._cancelled() or time.monotonic() >= deadline),
            DEFAULT_SQLITE_PROGRESS_INTERVAL,
        )

    def execute_readonly(self, sql: str, *, limit: int):
        """Apply engine-level restrictions in addition to the caller's SQL validation."""
        import sqlite3

        if limit < 0:
            raise ValueError("read-only queries require a non-negative row limit")
        conn = self._get_conn()
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA trusted_schema = OFF")
        old_length = conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64_000)
        functions = frozenset(
            {
                "count",
                "sum",
                "avg",
                "min",
                "max",
                "total",
                "abs",
                "round",
                "coalesce",
                "ifnull",
                "nullif",
                "iif",  # codespell:ignore iif
                "length",
                "lower",
                "upper",
                "trim",
                "ltrim",
                "rtrim",
                "substr",
                "substring",
                "replace",
                "instr",
                "like",
                "glob",
                "date",
                "time",
                "datetime",
                "julianday",
                "unixepoch",
                "strftime",
                "typeof",
                "hex",
                "group_concat",
                "row_number",
                "rank",
                "dense_rank",
                "percent_rank",
                "cume_dist",
                "ntile",
                "lag",
                "lead",
                "first_value",
                "last_value",
                "nth_value",
            }
        )

        def authorize(action, arg1, arg2, _database, _trigger):
            if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE}:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() in functions:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        conn.set_authorizer(authorize)
        try:
            return self.execute(sql, limit=limit)
        finally:
            conn.set_authorizer(None)
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, old_length)

    def _metadata_query(self, sql: str, parameters: tuple = ()) -> list[dict[str, Any]]:
        """Parameterized catalog query, sharing the normal connection and execution limits."""
        conn = self._get_conn()
        self._before_execute(conn)
        with contextlib.closing(conn.execute(sql, parameters)) as cursor:
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def search_objects(
        self,
        pattern: str = "",
        object_type: str = "table",
        *,
        offset: int = 0,
        limit: int = 20,
        exact: bool = False,
    ) -> dict[str, Any]:
        """Search SQLite names with LIKE or exact equality, preserving pagination."""
        if offset < 0 or limit <= 0:
            raise ValueError("offset must be non-negative and limit must be positive")
        if object_type == "table":
            sql = """SELECT name AS table_name, type AS object_type FROM sqlite_schema
                     WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite!_%' ESCAPE '!'
                       AND name LIKE ? ORDER BY name LIMIT ? OFFSET ?"""
        elif object_type == "column":
            sql = """SELECT s.name AS table_name, c.name AS column_name, c.type AS value_type
                     FROM sqlite_schema AS s JOIN pragma_table_xinfo(s.name) AS c
                     WHERE s.type IN ('table', 'view') AND s.name NOT LIKE 'sqlite!_%' ESCAPE '!'
                       AND c.hidden != 1 AND c.name LIKE ?
                     ORDER BY s.name, c.cid LIMIT ? OFFSET ?"""
        else:
            raise ValueError("object_type must be table or column")
        if exact:
            sql = sql.replace(" LIKE ?", " = ?")
        rows = self._metadata_query(sql, (pattern if exact else pattern or "%", limit + 1, offset))
        return {
            "objects": rows[:limit],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def describe_table(self, table: str) -> dict[str, Any]:
        """Read exact SQLite metadata, preserving composite and implicit foreign keys."""
        entries = self._metadata_query(
            "SELECT name, type, sql FROM sqlite_schema WHERE name = ? "
            "AND type IN ('table', 'view') AND name NOT LIKE 'sqlite!_%' ESCAPE '!'",
            (table,),
        )
        if not entries:
            raise UnknownTableError(f"Table or view does not exist: {table}")
        entry = entries[0]
        columns = {
            row["name"]: {
                "value_type": row["type"],
                "description": "",
                "not_null": bool(row["notnull"]),
                "primary_key_position": row["pk"],
                "default": row["dflt_value"],
            }
            for row in self._metadata_query("SELECT * FROM pragma_table_xinfo(?)", (table,))
            if row["hidden"] != 1
        }
        if self.config.descriptions_dir:
            directory = Path(self.config.descriptions_dir).resolve()
            file = (directory / (table + ".csv")).resolve()
            # Only an explicitly configured table sidecar can annotate real columns.
            if file.parent == directory and file.is_file():
                if file.stat().st_size > 1_000_000:
                    raise ValueError("Table description CSV exceeds 1 MB")
                raw = file.read_bytes()
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = raw.decode("cp1252")
                for row in csv.DictReader(io.StringIO(text)):
                    name = (row.get("original_column_name") or "").strip()
                    if name in columns:
                        parts = [
                            str(row.get(key) or "").strip()
                            for key in ("column_name", "column_description", "value_description")
                        ]
                        columns[name]["description"] = "; ".join(
                            dict.fromkeys(p for p in parts if p)
                        )[:6000]
        foreign_keys: dict[int, dict] = {}
        for row in self._metadata_query(
            "SELECT * FROM pragma_foreign_key_list(?) ORDER BY id, seq", (table,)
        ):
            fk = foreign_keys.setdefault(
                row["id"], {"table": row["table"], "columns": [], "referenced_columns": []}
            )
            target = row["to"]
            if target is None:
                pk = self._metadata_query(
                    "SELECT name FROM pragma_table_xinfo(?) WHERE pk > 0 ORDER BY pk",
                    (row["table"],),
                )
                target = pk[row["seq"]]["name"] if row["seq"] < len(pk) else None
            fk["columns"].append(row["from"])
            fk["referenced_columns"].append(target)
        indexes = []
        for row in self._metadata_query("SELECT * FROM pragma_index_list(?)", (table,)):
            index_columns = self._metadata_query(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (row["name"],)
            )
            indexes.append(
                {
                    "name": row["name"],
                    "unique": bool(row["unique"]),
                    "partial": bool(row["partial"]),
                    "columns": [column["name"] for column in index_columns],
                }
            )
        return {
            "table": table,
            "object_type": entry["type"],
            "description": "",
            "columns": columns,
            "foreign_keys": list(foreign_keys.values()),
            "indexes": indexes,
            "ddl": entry["sql"] or "",
        }


def build_sql_service(
    engine: str,
    config: dict[str, Any],
    *,
    timeout_seconds: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> SqlService:
    """Construct a service. Metadata, explicit deadlines and cancellation are SQLite-only."""
    if engine in {"sqlite", "sqlite3"}:
        return SQLiteService(
            SQLiteConfig(**config), timeout_seconds=timeout_seconds, cancelled=cancelled
        )
    if engine not in {"mysql", "gaussvector", "hive", "spark", "mcp"}:
        raise ValueError(f"Unsupported database engine: {engine}")
    if timeout_seconds is not None or cancelled is not None:
        raise ValueError("Explicit deadlines and cancellation are supported only for SQLite")
    if engine == "mysql":
        return MySQLService(MySQLConfig(**config))
    if engine == "gaussvector":
        return GaussVectorService(GaussVectorConfig(**config))
    if engine == "mcp":
        from ._mcp_sql import MCPSqlService

        try:
            return MCPSqlService(MCPConfig(**config))
        except (TypeError, ValueError) as exc:
            raise MCPInfrastructureError(exc) from exc
    return SparkService(SparkConfig(**config))
