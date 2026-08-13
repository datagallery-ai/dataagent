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
"""SQLGlot-based security checker with no model or database calls."""

from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, OptimizeError
from sqlglot.optimizer.qualify import qualify

from dataagent.agents.nl2sql.security.models import SecurityCheckResult, SecurityViolation


def check_sql(sql: str, *, dialect: str, schema: dict[str, Any]) -> SecurityCheckResult:
    """Check one SQL candidate using deterministic SQLGlot rules."""
    from dataagent.agents.nl2sql.security.rules import (
        check_allowed_functions,
        check_allowed_query_syntax,
        check_resource_usage,
        check_semantic_schema,
    )

    if len((sql or "").encode("utf-8")) > 1024 * 1024:
        return SecurityCheckResult([SecurityViolation("RESOURCE-001", "SQL text exceeds 1 MiB.")])
    try:
        statements = sqlglot.parse(sql, read=dialect, error_level=ErrorLevel.RAISE)
    except Exception as exc:
        return SecurityCheckResult([SecurityViolation("SQL-001", f"SQL parse failed: {exc}")])
    executable = [statement for statement in statements if statement is not None]
    if len(executable) != 1:
        return SecurityCheckResult([SecurityViolation("SQL-001", "Exactly one SQL statement is allowed.")])
    allowed = (exp.Select, exp.Union, exp.Except, exp.Intersect)
    if not isinstance(executable[0], allowed):
        return SecurityCheckResult([SecurityViolation("SQL-001", "Only read-only SELECT queries are allowed.")])
    statement = executable[0]
    if any(not select.expressions for select in statement.find_all(exp.Select)):
        return SecurityCheckResult([SecurityViolation("SQL-001", "Every SELECT must contain a projection.")])
    violations = _check_read_only_structure(statement)
    if violations:
        return SecurityCheckResult(violations)
    violations.extend(check_resource_usage(statement))
    if violations:
        return SecurityCheckResult(violations)
    violations.extend(check_allowed_query_syntax(statement))
    if violations:
        return SecurityCheckResult(violations)
    violations.extend(check_allowed_functions(statement, sql=sql, dialect=dialect))
    violations.extend(check_semantic_schema(statement, dialect=dialect, schema=schema))
    return SecurityCheckResult(violations)


def qualify_with_semantic_schema(
    statement: exp.Expression,
    *,
    dialect: str,
    schema: dict[str, Any],
) -> exp.Expression:
    """Qualify table aliases, columns, and stars using semantic metadata."""
    sqlglot_schema: dict[str, Any] = {}
    for table_name, table_meta in schema.items():
        columns = dict.fromkeys(table_meta.get("columns", {}), "UNKNOWN")
        _insert_sqlglot_schema(sqlglot_schema, str(table_name), columns)
    try:
        return qualify(
            statement.copy(),
            dialect=dialect,
            schema=sqlglot_schema,
            identify=False,
            validate_qualify_columns=True,
        )
    except OptimizeError as exc:
        raise ValueError(str(exc)) from exc


def _insert_sqlglot_schema(target: dict[str, Any], table_name: str, columns: dict[str, str]) -> None:
    parts = [part for part in table_name.split(".") if part]
    current = target
    for qualifier in parts[:-1]:
        child = current.get(qualifier, {})
        if not isinstance(child, dict):
            child = {}
        current.update({qualifier: child})
        current = child
    if parts:
        current.update({parts[-1]: columns})


def _check_read_only_structure(statement: exp.Expression) -> list[SecurityViolation]:
    forbidden_names = (
        "Insert",
        "Update",
        "Delete",
        "Merge",
        "TruncateTable",
        "Create",
        "Alter",
        "Drop",
        "Comment",
        "Grant",
        "Revoke",
        "Copy",
        "Command",
        "Transaction",
        "Commit",
        "Rollback",
        "Set",
    )
    forbidden_types = []
    for name in forbidden_names:
        node_type = getattr(exp, name, None)
        if node_type is not None:
            forbidden_types.append(node_type)
    if forbidden_types and statement.find(*forbidden_types):
        return [SecurityViolation("SQL-002", "Write, DDL, control, and maintenance operations are not allowed.")]
    for select in statement.find_all(exp.Select):
        if select.args.get("into") is not None:
            return [SecurityViolation("SQL-002", "SELECT INTO is not allowed.")]
        if select.args.get("locks"):
            return [SecurityViolation("SQL-002", "Row-locking SELECT queries are not allowed.")]
    return []
