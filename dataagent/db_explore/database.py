"""Per-run exploration policy and context assembly over the shared SQL service."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from typing import Literal

import sqlglot
from sqlglot import exp

from dataagent.data_gateway.schema import SchemaContext
from dataagent.data_gateway.sql_security.checker import check_sql
from dataagent.data_gateway.sql_service import build_sql_service
from dataagent.data_gateway.sqlite_sql import render_sqlite_sql


class DatabaseExplorer:
    """Keep recall state here; delegate connections and physical metadata to SqlService.

    Each tool uses a short-lived service in its worker thread. The service owns
    its connection, timeout and cancellation; recall owns the task-wide budget.
    """

    def __init__(
        self,
        engine: str,
        connection_config: dict,
        *,
        dialect: str,
        max_rows: int,
        max_tables: int,
        query_timeout_seconds: float,
        deadline: float,
    ):
        self.engine = engine
        self.dialect = dialect
        self._connection_config = dict(connection_config)
        self.max_rows = max_rows
        self.max_tables = max_tables
        self.query_timeout = query_timeout_seconds
        self.deadline = deadline
        self.cancelled = threading.Event()
        self.tables: dict[str, dict] = {}
        self._tables_lock = threading.Lock()
        self._pending_tables: set[str] = set()

    @contextmanager
    def _service(self):
        remaining = min(self.query_timeout, self.deadline - time.monotonic())
        if remaining <= 0 or self.cancelled.is_set():
            raise TimeoutError("Database recall deadline exceeded")
        with build_sql_service(
            self.engine,
            self._connection_config,
            timeout_seconds=remaining,
            cancelled=self.cancelled.is_set,
        ) as service:
            yield service

    def search_objects(
        self, pattern: str = "", object_type: Literal["table", "column"] = "table", offset: int = 0
    ) -> dict:
        """Search SQLite table/view or column names with SQL LIKE (% wildcard); paginate with offset."""
        with self._service() as service:
            return service.search_objects(pattern, object_type, offset=offset, limit=self.max_rows)

    def describe_table(self, table: str) -> dict:
        """Inspect exact columns, declared foreign keys, uniqueness, indexes and DDL for one table/view."""
        with self._tables_lock:
            if table in self.tables:
                return self.tables[table]
            if table in self._pending_tables:
                raise ValueError(f"Table inspection already in progress: {table}")
            if len(self.tables) + len(self._pending_tables) >= self.max_tables:
                raise ValueError(f"Recall table budget reached ({self.max_tables})")
            self._pending_tables.add(table)
        try:
            with self._service() as service:
                result = service.describe_table(table)
            with self._tables_lock:
                self.tables[table] = result
                self._pending_tables.remove(table)
            return result
        finally:
            with self._tables_lock:
                self._pending_tables.discard(table)

    def inspected_schema(self) -> dict:
        with self._tables_lock:
            return {name: {"columns": meta["columns"]} for name, meta in self.tables.items()}

    def discard_table(self, table: str) -> None:
        """Discard only this tool's oversized metadata, leaving other parallel results intact."""
        with self._tables_lock:
            self.tables.pop(table, None)

    def probe_sql(self, sql: str, *, schema: dict | None = None) -> dict:
        """Check values, ranges or join cardinality using inspected tables only."""
        schema = self.inspected_schema() if schema is None else schema
        try:
            statements = [
                statement
                for statement in sqlglot.parse(sql, read=self.dialect)
                if statement is not None
            ]
        except sqlglot.errors.SqlglotError as exc:
            raise ValueError(f"Invalid probe SQL: {exc}") from exc
        if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union)):
            raise ValueError("Probe requires exactly one read-only SELECT")
        statement = statements[0]
        if statement.args.get("limit") is None:
            statement = statement.limit(self.max_rows + 1)
        sql = (
            render_sqlite_sql(statement)
            if self.dialect == "sqlite"
            else statement.sql(dialect=self.dialect)
        )
        checked = check_sql(sql, dialect=self.dialect, schema=schema)
        if checked.violations:
            raise ValueError("; ".join(v.message for v in checked.violations))
        sql = (checked.normalized_sql or sql).strip().rstrip(";")
        with self._service() as service:
            columns, rows, error = service.execute(
                f'SELECT * FROM ({sql}\n) AS "_recall_probe" LIMIT {self.max_rows + 1}',
                limit=self.max_rows,
            )
        if error is not None:
            raise ValueError(error)
        clipped = False
        values = []
        for row in rows[: self.max_rows]:
            out = []
            for value in row:
                if isinstance(value, bytes):
                    value = f"<binary: {len(value)} bytes>"
                    clipped = True
                elif isinstance(value, str) and len(value) > 256:
                    value = value[:256] + "…"
                    clipped = True
                out.append(value)
            values.append(out)
        return {
            "columns": columns,
            "rows": values,
            "truncated": len(rows) > self.max_rows,
            "cells_truncated": clipped,
        }

    def context(self, selected: list[str]) -> SchemaContext:
        """Build downstream context exclusively from tables actually inspected."""
        if not selected or len(selected) != len(set(selected)):
            raise ValueError("Select at least one distinct inspected table")
        unknown = set(selected) - self.tables.keys()
        if unknown:
            raise ValueError(f"Tables have not been inspected: {sorted(unknown)}")
        schema, joins, blocks = {}, [], []
        for name in selected:
            meta = self.tables[name]
            schema[name] = {"description": meta["description"], "columns": meta["columns"]}
            blocks.append(
                meta["ddl"]
                + "\n-- Inspected columns and indexes:\n"
                + json.dumps(
                    {"columns": meta["columns"], "indexes": meta["indexes"]}, ensure_ascii=False
                )
            )
            for fk in meta["foreign_keys"]:
                target = fk["table"]
                if target in selected:
                    for column, target_column in zip(
                        fk["columns"], fk["referenced_columns"], strict=True
                    ):
                        if target_column in self.tables[target]["columns"]:
                            joins.append((f"{name}.{column}", f"{target}.{target_column}"))
        return SchemaContext(schema=schema, joins=joins, schema_str="\n\n".join(blocks))
