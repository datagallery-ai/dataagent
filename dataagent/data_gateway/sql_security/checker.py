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

from functools import cache
from typing import Any, Literal, cast

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel
from sqlglot.expressions import Expression

from dataagent.data_gateway.sql_security.models import SecurityCheckResult, SecurityViolation
from dataagent.data_gateway.sql_security.rules import (
    check_allowed_functions,
    check_allowed_query_syntax,
    check_resource_usage,
    check_semantic_schema,
    resolve_grouping_aliases,
)
from dataagent.data_gateway.sql_security.semantic_resolver import (
    normalize_semantic_column_references,
)


def check_sql(
    sql: str,
    *,
    dialect: str,
    schema: dict[str, Any],
    policy: Literal["gateway", "nl2sql"] = "gateway",
) -> SecurityCheckResult:
    """Check one query using the Gateway or NL2SQL workflow policy.

    The NL2SQL policy retains the workflow's grouping and join rules and
    skips source-column checks against semantic metadata.
    """

    if len((sql or "").encode("utf-8")) > 1024 * 1024:
        return SecurityCheckResult([SecurityViolation("RESOURCE-001", "SQL text exceeds 1 MiB.")])
    try:
        statement = _parse_sql(sql, dialect)
    except Exception as exc:
        return SecurityCheckResult([SecurityViolation("SQL-001", f"SQL parse failed: {exc}")])
    if isinstance(statement, SecurityViolation):
        return SecurityCheckResult([statement])
    try:
        return _check_statement(statement, sql, dialect, schema, nl2sql_policy=policy == "nl2sql")
    except RecursionError:
        return SecurityCheckResult(
            [SecurityViolation("RESOURCE-002", "SQL boolean expression exceeds evaluation depth.")]
        )


def check_read_only_sql(sql: str, *, dialect: str) -> SecurityCheckResult:
    """Require a single read-only query without applying the additional policy rules."""
    try:
        statement = _parse_sql(sql, dialect)
    except (sqlglot.errors.SqlglotError, RecursionError) as exc:
        return SecurityCheckResult([SecurityViolation("SQL-001", str(exc))])
    if isinstance(statement, SecurityViolation):
        return SecurityCheckResult(
            [SecurityViolation(statement.rule_id, "Only one read-only SELECT is allowed.")]
        )
    return SecurityCheckResult(_check_read_only_structure(statement))


def _parse_sql(sql: str, dialect: str) -> Expression | SecurityViolation:
    statements = sqlglot.parse(sql, read=dialect, error_level=ErrorLevel.RAISE)
    executable = [statement for statement in statements if statement is not None]
    if len(executable) != 1:
        return SecurityViolation("SQL-001", "Exactly one SQL statement is allowed.")
    allowed = (exp.Select, exp.Union, exp.Except, exp.Intersect)
    if not isinstance(executable[0], allowed):
        return SecurityViolation("SQL-001", "Only read-only SELECT queries are allowed.")
    return executable[0]


def _check_statement(
    statement: Expression, sql: str, dialect: str, schema: dict[str, Any], *, nl2sql_policy: bool
) -> SecurityCheckResult:
    if any(not select.expressions for select in statement.find_all(exp.Select)):
        return SecurityCheckResult(
            [SecurityViolation("SQL-001", "Every SELECT must contain a projection.")]
        )
    normalized_sql = normalize_semantic_column_references(
        statement, sql=sql, dialect=dialect, schema=schema
    )
    if normalized_sql is not None:
        try:
            statement = cast(
                Any, sqlglot.parse_one(normalized_sql, read=dialect, error_level=ErrorLevel.RAISE)
            )
        except Exception as exc:
            return SecurityCheckResult(
                [SecurityViolation("SQL-001", f"SQL normalization failed: {exc}")]
            )
        sql = normalized_sql
    violations = _check_read_only_structure(statement)
    if violations:
        return SecurityCheckResult(violations)

    @cache
    def grouping_statement() -> Expression:
        return resolve_grouping_aliases(statement, dialect=dialect, schema=schema)

    grouping = grouping_statement if nl2sql_policy else None
    violations.extend(
        check_resource_usage(statement, nl2sql_policy=nl2sql_policy, grouping_statement=grouping)
    )
    if violations:
        return SecurityCheckResult(violations)
    violations.extend(
        check_allowed_query_syntax(
            statement, nl2sql_policy=nl2sql_policy, grouping_statement=grouping
        )
    )
    if violations:
        return SecurityCheckResult(violations)
    violations.extend(check_allowed_functions(statement, sql=sql, dialect=dialect))
    if violations:
        return SecurityCheckResult(violations)
    violations.extend(
        check_semantic_schema(
            statement, dialect=dialect, schema=schema, validate_columns=not nl2sql_policy
        )
    )
    return SecurityCheckResult(
        violations, normalized_sql=normalized_sql if not violations else None
    )


def _check_read_only_structure(statement: Expression) -> list[SecurityViolation]:
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
        return [
            SecurityViolation(
                "SQL-002", "Write, DDL, control, and maintenance operations are not allowed."
            )
        ]
    for select in statement.find_all(exp.Select):
        if select.args.get("into") is not None:
            return [SecurityViolation("SQL-002", "SELECT INTO is not allowed.")]
        if select.args.get("locks"):
            return [SecurityViolation("SQL-002", "Row-locking SELECT queries are not allowed.")]
    return []
