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
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger

from dataagent.actions.environment.env import Env


class BaseNL2SQLEnv(Env):
    """
    BaseNL2SQLEnv environment wrapper.

    Metadata schema:
    - table_metadata:
        table_name: str
        table_description: str
        columns: list[str]
        embedding: FLOAT[]  (table_description embedding)

    - column_metadata:
        column_name: str
        table_name: str
        column_type: str
        column_description: str
        value_examples: list
        column_embedding: FLOAT[] (column_description embedding)
    """

    def __init__(
        self,
        db_path: str,
        table_metadata_table: str = "table_metadata",
        column_metadata_table: str = "column_metadata",
    ):
        self.db_path = str(db_path)

        self.table_metadata_table = table_metadata_table
        self.column_metadata_table = column_metadata_table
        self._conn: Any | None = None

        super().__init__()

    @property
    def conn(self):
        """Ensure connection to the database."""
        if self._conn is None:
            import duckdb

            self._conn = duckdb.connect(self.db_path, read_only=True)
        return self._conn

    @staticmethod
    def _cursor_columns(cursor: Any) -> list[str]:
        """Extract column names from a DB-API cursor."""
        if getattr(cursor, "description", None):
            return [col[0] for col in cursor.description]
        return []

    @staticmethod
    def _is_empty(v: Any) -> bool:
        """Check if a value is None or an empty string."""
        if v is None:
            return True
        return isinstance(v, str) and v.strip() == ""

    @staticmethod
    def _quote_ident(name: str) -> str:
        """Safely quote SQL identifiers."""
        return '"' + str(name).replace('"', '""') + '"'

    @staticmethod
    def _normalize_column_name(
        col_name: str | None,
        table_name: str | None = None,
    ) -> str:
        """Normalize column name by removing table prefix if present."""
        if not col_name:
            return ""
        s = str(col_name)

        if table_name:
            prefix = f"{table_name}."
            if s.startswith(prefix):
                return s[len(prefix) :]

        parts = s.split(".")
        if len(parts) >= 3:
            return ".".join(parts[2:])
        if len(parts) == 2:
            return parts[1]
        return s

    @staticmethod
    def _parse_list_field(v: Any) -> list[Any]:
        """Parse list-like fields returned by the metadata tables."""
        if v is None:
            return []
        if isinstance(v, list):
            return v
        if isinstance(v, tuple):
            return list(v)
        if isinstance(v, np.ndarray):
            return v.tolist()
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, list):
                return obj
            if "," in s:
                return [x.strip() for x in s.split(",") if x.strip()]
            return [s]
        return [v]

    @staticmethod
    def _strip_sql(sql: str) -> str:
        """Strip whitespace and trailing semicolons from SQL query."""
        s = (sql or "").strip()
        while s.endswith(";"):
            s = s[:-1].rstrip()
        return s

    @staticmethod
    def _md_escape(s: str) -> str:
        """Escape pipes and normalize newlines for Markdown tables."""
        return s.replace("|", "\\|").replace("\n", "<br>").replace("\r", "")

    @staticmethod
    def _build_markdown_table(cols: list[str], rows: list[tuple], escape_func=None) -> str:
        """Build a Markdown table from columns and rows."""
        if escape_func is None:
            escape_func = BaseNL2SQLEnv._md_escape

        if not cols:
            return "**No columns found.**"
        if not rows:
            return "**No rows returned.**"

        header = "| " + " | ".join(escape_func(str(c)) for c in cols) + " |"
        sep = "| " + " | ".join(["---"] * len(cols)) + " |"

        body_lines = []
        for row in rows:
            vals = []
            for value in row:
                vals.append("NULL" if value is None else escape_func(str(value)))
            body_lines.append("| " + " | ".join(vals) + " |")

        return "\n".join([header, sep, *body_lines])

    @staticmethod
    def _tool_response(original_msg: str, frontend_msg: str | None = None) -> dict[str, str]:
        """DataAgent tool return shape for Executor normalization."""
        return {
            "original_msg": original_msg,
            "frontend_msg": frontend_msg if frontend_msg is not None else original_msg,
        }

    # ========= Env lifecycle =========

    def init(self):
        """Initialize the environment by validating the database path."""
        if not Path(self.db_path).exists():
            raise FileNotFoundError(f"DB file does not exist: {self.db_path}")

    def close(self):
        """Close the database connection."""
        try:
            if self._conn:
                self._conn.close()
        except Exception as exc:
            logger.warning("Failed to close the database connection: %s", str(exc))
        finally:
            self._conn = None

    def get_description(self) -> str:
        """Return all tables in the database with their descriptions."""
        if not self._table_exists(self.table_metadata_table):
            raise RuntimeError("Table metadata does not exist")

        rows = self._execute(
            f"""
            SELECT table_name, table_description
            FROM {self.table_metadata_table}
            ORDER BY table_name
            """
        ).fetchall()

        if not rows:
            return ""

        description = "### Database\n"
        description += "The database contains the following tables:\n"
        for table_name, table_desc in rows:
            description += f"{table_name}: {table_desc}\n"

        return description

    @Env.tool
    def get_table_metadata(self, table_name: str) -> dict[str, str]:
        """Retrieve metadata for a given table.

        Args:
            table_name: Table name.

        Returns:
            Table metadata in Dict.
        """
        result = self._extract_table_metadata_dict(table_name)
        if result.get("error"):
            logger.error(f"{result['error']}")
            error = f"**Error:** {result['error']}"
            return self._tool_response(error)

        original = json.dumps(result, ensure_ascii=False, indent=2)
        column_count = len(result.get("columns") or [])
        frontend = f"Loaded metadata for table `{table_name}` ({column_count} columns)."
        return self._tool_response(original, frontend)

    @Env.tool
    def is_sql_executable(self, sql: str) -> dict[str, str]:
        """
        Validates if a SQL query is executable.

        Args:
            sql: SQL query to be tested.

        Returns:
            dict with ``original_msg`` / ``frontend_msg``: "OK" on success,
            or an error message on failure.
        """
        s = self._strip_sql(sql)
        if not s:
            return self._tool_response("Error: Empty SQL")

        try:
            self._execute(f"EXPLAIN {s}")
            return self._tool_response("OK")
        except Exception as exc:
            return self._tool_response("Error: " + str(exc))

    @Env.tool
    def get_sample_rows(self, table_name: str, n: int = 5) -> dict[str, str]:
        """
        Retrieves N sample rows from a table.

        Args:
            table_name: Target table name.
            n: Number of sample rows to fetch.

        Returns:
            Markdown formatted table string. If error, returns a Markdown error message.
        """
        try:
            safe_table = self._quote_ident(table_name)
            n_int = max(0, int(n))

            cur = self._execute(f"SELECT * FROM {safe_table} LIMIT ?", (n_int,))
            rows = cur.fetchall()
            cols = self._cursor_columns(cur)

            if not cols:
                msg = f"**No columns found for table:** `{self._md_escape(table_name)}`"
                return self._tool_response(msg)
            if not rows:
                msg = f"**No rows returned for table:** `{self._md_escape(table_name)}` (limit={n_int})"
                return self._tool_response(msg)

            table_md = self._build_markdown_table(cols, rows)
            frontend = f"Returned {len(rows)} sample row(s) from `{table_name}` (limit={n_int})."
            return self._tool_response(table_md, frontend)

        except Exception as exc:
            msg = f"**Error:** `get_sample_rows` failed: {self._md_escape(str(exc))}"
            return self._tool_response(msg)

    @Env.tool
    def test_sql(self, sql: str, n: int = 5) -> dict[str, str]:
        """
        Executes a SQL query and returns up to n sample rows.

        Args:
            sql: The SQL query to be tested.
            n: Maximum number of sample rows to return.

        Returns:
            Markdown formatted table string. If error, returns a Markdown error message.
        """
        s = self._strip_sql(sql)
        if not s:
            error = "**Error:** Empty SQL"
            return self._tool_response(error)

        head = s.lstrip().lower()
        if not (head.startswith("select") or head.startswith("with")):
            error = "**Error:** `test_sql` only supports SELECT/WITH queries"
            return self._tool_response(error)

        try:
            lim = max(0, int(n))
            q = f"SELECT * FROM ({s}) AS __sub LIMIT ?"
            cur = self._execute(q, (lim,))
            rows = cur.fetchall()
            cols = self._cursor_columns(cur)

            if not cols:
                msg = "**No columns returned.**"
                return self._tool_response(msg)
            if not rows:
                msg = f"**No rows returned.** (limit={lim})"
                return self._tool_response(msg)

            table_md = self._build_markdown_table(cols, rows)
            frontend = f"Query succeeded with {len(rows)} preview row(s) (limit={lim})."
            return self._tool_response(table_md, frontend)

        except Exception as e:
            msg = f"**Error:** `test_sql` failed: {self._md_escape(str(e))}"
            return self._tool_response(msg)

    def _execute(self, sql: str, params: Sequence[Any] | None = None):
        """Execute SQL on the backend connection."""
        if params is None:
            return self.conn.execute(sql)
        return self.conn.execute(sql, list(params))

    def _table_exists(self, name: str) -> bool:
        """Check if a table exists in the database."""
        try:
            rows = self._execute("SHOW TABLES").fetchall()
            return name in [r[0] for r in rows]
        except Exception:
            return False

    def _extract_table_metadata_dict(self, table_name: str) -> dict[str, Any]:
        """
        Return dict strictly matching get_table_metadata docstring:
        {
            "table_name": xxx,
            "table_description": xxx,
            "primary_keys": [xxx, ...],
            "joinable_keys": [xxx, ...],
            "columns": [
                {
                    "column_name": xxx,
                    "column_description": xxx,
                    "column_type": xxx,
                    "value_examples": [xxx, ...]
                }, ...
            ]
        }
        """
        if not self._table_exists(self.table_metadata_table):
            return {"error": f"Cannot find table '{self.table_metadata_table}'."}
        if not self._table_exists(self.column_metadata_table):
            return {"error": f"Cannot find table '{self.column_metadata_table}'."}

        row = self._execute(
            f"""
            SELECT
                table_name,
                table_description,
                columns,
                primary_keys,
                joinable_keys
            FROM {self.table_metadata_table}
            WHERE table_name = ?
            LIMIT 1
            """,
            (table_name,),
        ).fetchone()

        if not row:
            return {"error": f"Cannot find metadata for table '{table_name}' in {self.table_metadata_table}"}

        tb_name, tb_desc, cols_field, pk_field, fk_field = row

        col_names = [str(x) for x in self._parse_list_field(cols_field) if not self._is_empty(x)]
        pk_list = [str(x) for x in self._parse_list_field(pk_field) if not self._is_empty(x)]
        fk_list = [str(x) for x in self._parse_list_field(fk_field) if not self._is_empty(x)]

        columns_payload: list[dict[str, Any]] = []
        if col_names:
            placeholders = ",".join(["?"] * len(col_names))
            crows = self._execute(
                f"""
                SELECT column_name, column_description, column_type, value_examples
                FROM {self.column_metadata_table}
                WHERE table_name = ?
                AND column_name IN ({placeholders})
                """,
                [tb_name, *col_names],
            ).fetchall()

            cmap: dict[str, dict[str, Any]] = {}
            for cn, cd, ct, ve in crows:
                cmap[str(cn)] = {
                    "column_name": self._normalize_column_name(cn, table_name=tb_name),
                    "column_description": cd or "",
                    "column_type": ct or "",
                    "value_examples": self._parse_list_field(ve),
                }

            for cn in col_names:
                columns_payload.append(
                    cmap.get(
                        cn,
                        {
                            "column_name": self._normalize_column_name(cn, table_name=tb_name),
                            "column_description": "",
                            "column_type": "",
                            "value_examples": [],
                        },
                    )
                )

        return {
            "table_name": str(tb_name),
            "table_description": tb_desc or "",
            "primary_keys": pk_list,
            "joinable_keys": fk_list,
            "columns": columns_payload,
        }
