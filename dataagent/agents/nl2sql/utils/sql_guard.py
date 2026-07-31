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
"""Independent SQLGlot safety gate for NL2SQL explain/execute paths."""

from __future__ import annotations

import contextlib
from typing import Any

from dataagent.agents.nl2sql.utils.sql_rules import RuleContext, Violation, run_ast_rules

# Classic engine-abuse helpers only. No DuckDB read_*/_scan shape rules —
# product NL2SQL paths do not target DuckDB as the SQL service dialect.
_DANGEROUS_FUNCTION_NAMES = frozenset(
    {
        "pg_sleep",
        "sleep",
        "benchmark",
        "load_file",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "dblink",
        "dblink_exec",
        "lo_import",
        "lo_export",
        "dbms_lock",
        "utl_http",
        "utl_file",
        "xp_cmdshell",
        # SQLite: load arbitrary native extensions.
        "load_extension",
    }
)


def _is_dangerous_function_name(name: str) -> bool:
    """Return True if ``name`` is on the dangerous-function blacklist."""
    return bool(name) and name in _DANGEROUS_FUNCTION_NAMES


class SQLGuardError(ValueError):
    """Raised when SQL fails the independent safety gate."""

    def __init__(self, message: str, *, violations: list[Violation] | None = None) -> None:
        """Store message and optional structured rule violations."""
        super().__init__(message)
        self.violations = list(violations or [])

    def to_dict(self) -> dict[str, Any]:
        """Serialize error message and violations for API / logs."""
        return {
            "message": str(self),
            "violations": [v.to_dict() for v in self.violations],
        }


def resolve_sqlglot_dialect(engine: str | None) -> str | None:
    """Map DataAgent engine names to sqlglot read dialects."""
    if not engine:
        return None
    if engine == "gaussvector":
        return "postgres"
    if engine in {"sqlite", "sqlite3"}:
        return "sqlite"
    if engine in {"hive", "spark"}:
        return "spark"
    return engine


def guard_sql(
    sql: str,
    *,
    dialect: str | None = None,
    read_only: bool = True,
) -> None:
    """Parse and reject multi-statement, writes, dangerous constructs, and AST rules.

    ``sqlglot`` missing is a hard failure (never silently skipped).
    Registered ``sql_rules`` (R1–R3, …) run on the same parsed AST before return;
    callers on explain/execute paths must invoke this before DB work.

    Dangerous constructs such as INTO OUTFILE / LOAD DATA / COPY TO are enforced via
    parse failure or AST statement shape — never via raw-text regex (avoids comment /
    literal false positives).
    """
    text = (sql or "").strip()
    if not text:
        raise SQLGuardError("Empty SQL is not allowed.")

    try:
        import sqlglot
        from sqlglot import exp
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise SQLGuardError("sqlglot is required for SQL safety checks and is not installed.") from exc

    try:
        statements = sqlglot.parse(text, read=dialect, error_level=sqlglot.errors.ErrorLevel.RAISE)
    except Exception as exc:
        raise SQLGuardError(f"SQL parse failed: {exc}") from exc

    statements = [stmt for stmt in statements if stmt is not None]
    if not statements:
        raise SQLGuardError("SQL parse produced no statements.")
    if len(statements) > 1:
        raise SQLGuardError("Multi-statement SQL is not allowed.")

    parsed = statements[0]
    allowed: tuple[Any, ...] = (exp.Select, exp.Union, exp.Except, exp.Intersect)
    forbidden: tuple[Any, ...] = (
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Create,
        exp.Drop,
        exp.Alter,
        exp.Merge,
        exp.Command,
        exp.Transaction,
        exp.Commit,
        exp.Rollback,
        exp.Set,
        exp.Use,
        exp.TruncateTable,
        exp.Copy,
    )

    if read_only:
        if not isinstance(parsed, allowed):
            raise SQLGuardError(f"Only read-only statements are allowed; got {type(parsed).__name__}.")
        if parsed.find(*forbidden):
            raise SQLGuardError("Only read-only statements are allowed. Write operations are forbidden.")
        # Postgres SELECT INTO / TEMP is a write even when nested under UNION branches.
        for select in parsed.find_all(exp.Select):
            if select.args.get("into") is not None:
                raise SQLGuardError("SELECT INTO write target is not allowed in read-only mode.")
            # FOR UPDATE / FOR SHARE take row locks; not read-only for NL2SQL.
            if select.args.get("locks"):
                raise SQLGuardError("Row locking clauses (FOR UPDATE / FOR SHARE) are not allowed in read-only mode.")

    _reject_dangerous_functions(parsed, exp)

    violations = run_ast_rules(parsed, RuleContext(dialect=dialect))
    if violations:
        summary = "; ".join(f"{v.rule_id}: {v.message}" for v in violations)
        raise SQLGuardError(f"SQL security rule violation: {summary}", violations=violations)


def _normalize_function_name(raw: Any) -> str:
    """Normalize function identifiers (strip quotes/backticks) before blacklist match."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        name = raw
    else:
        name = getattr(raw, "name", None)
        if not isinstance(name, str) or not name:
            inner = getattr(raw, "this", raw)
            if isinstance(inner, str):
                name = inner
            elif inner is not None and inner is not raw:
                return _normalize_function_name(inner)
            else:
                name = str(raw)
    name = name.strip().lower()
    while len(name) >= 2 and name[0] == name[-1] and name[0] in "\"'`":
        name = name[1:-1].strip()
    return name.strip("\"'`")


def _reject_dangerous_functions(parsed: Any, exp: Any) -> None:
    """Reject known dangerous function names in the AST."""
    for node in parsed.find_all(exp.Anonymous):
        name = _normalize_function_name(getattr(node, "this", None))
        if _is_dangerous_function_name(name):
            raise SQLGuardError(f"Dangerous SQL function is not allowed: {name}")
    for node in parsed.find_all(exp.Func):
        name = ""
        sql_name = getattr(node, "sql_name", None)
        if callable(sql_name):
            with contextlib.suppress(Exception):
                name = _normalize_function_name(sql_name())
        if not name:
            name = _normalize_function_name(type(node).__name__)
        if _is_dangerous_function_name(name):
            raise SQLGuardError(f"Dangerous SQL function is not allowed: {name}")
