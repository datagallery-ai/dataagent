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
"""Fixed SQL security rules evaluated with SQLGlot."""

# ruff: noqa: UP045

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlglot import Tokenizer, exp
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.tokens import TokenType

from dataagent.agents.nl2sql.security.models import SecurityViolation

_ALLOWED_FUNCTIONS = frozenset(
    {
        "abs",
        "avg",
        "case",
        "cast",
        "ceil",
        "ceiling",
        "coalesce",
        "concat",
        "concat_ws",
        "count",
        "current_date",
        "current_timestamp",
        "date_trunc",
        "extract",
        "floor",
        "generate_series",
        "hex",
        "instr",
        "length",
        "lengthb",
        "lower",
        "lpad",
        "ltrim",
        "max",
        "min",
        "now",
        "nullif",
        "nvl",
        "replace",
        "round",
        "rpad",
        "rtrim",
        "substr",
        "sum",
        "to_char",
        "to_date",
        "to_number",
        "trim",
        "upper",
    }
)
_MAX_STRING_BYTES = 16 * 1024 * 1024


def _resolve_expression_types(names: tuple[str, ...]) -> tuple[type[Any], ...]:
    node_types = []
    for name in names:
        node_type = getattr(exp, name, None)
        if node_type is not None:
            node_types.append(node_type)
    return tuple(node_types)


_CONTEXT_FUNCTION_TYPES = _resolve_expression_types(
    (
        "CurrentAccount",
        "CurrentAccountName",
        "CurrentAvailableRoles",
        "CurrentCatalog",
        "CurrentClient",
        "CurrentDatabase",
        "CurrentDatetime",
        "CurrentIpAddress",
        "CurrentOrganizationName",
        "CurrentOrganizationUser",
        "CurrentRegion",
        "CurrentRole",
        "CurrentRoleType",
        "CurrentSchema",
        "CurrentSchemas",
        "CurrentSecondaryRoles",
        "CurrentSession",
        "CurrentStatement",
        "CurrentTime",
        "CurrentTimestampLTZ",
        "CurrentTimezone",
        "CurrentTransaction",
        "CurrentUser",
        "CurrentUserId",
        "CurrentVersion",
        "CurrentWarehouse",
        "Localtime",
        "Localtimestamp",
        "SessionUser",
    )
)

_FORBIDDEN_QUERY_TYPES = _resolve_expression_types(
    (
        "Array",
        "Between",
        "Escape",
        "Except",
        "Fetch",
        "ILike",
        "Intersect",
        "Lateral",
        "Offset",
        "RegexpLike",
        "RLike",
        "SimilarTo",
        "TableSample",
        "Values",
        "Window",
    )
)


def check_allowed_functions(
    statement: exp.Expression,
    *,
    sql: str,
    dialect: str,
) -> list[SecurityViolation]:
    """Require every SQL function to use an explicitly allowed source spelling."""
    # SQLGlot normalizes equivalent functions in the AST, so inspect source tokens to preserve names such as
    # SUBSTR versus SUBSTRING and EXTRACT versus DATE_PART.
    tokens = Tokenizer(dialect=dialect).tokenize(sql)
    for index, token in enumerate(tokens[:-1]):
        if tokens[index + 1].token_type is not TokenType.L_PAREN:
            continue
        if token.token_type not in {TokenType.IDENTIFIER, TokenType.VAR}:
            continue
        name = _normalize_identifier(token.text)
        quoted = token.token_type is TokenType.IDENTIFIER
        qualified = index > 0 and tokens[index - 1].token_type is TokenType.DOT
        if quoted or qualified or name not in _ALLOWED_FUNCTIONS:
            return [SecurityViolation("FUNCTION-001", f"SQL function is not in the allowlist: {name}.")]
    # Some keyword-shaped function names use dedicated token types. Their source spans remain available on the AST.
    for node in statement.find_all(exp.Func):
        start = node.meta.get("start")
        end = node.meta.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        name = _normalize_identifier(sql[start : end + 1])
        namespace = _function_namespace(node)
        if namespace or name not in _ALLOWED_FUNCTIONS:
            qualified_name = f"{namespace}.{name}" if namespace else name
            return [SecurityViolation("FUNCTION-001", f"SQL function is not in the allowlist: {qualified_name}.")]
    context_function = statement.find(*_CONTEXT_FUNCTION_TYPES)
    if context_function is not None:
        name = _function_name(context_function)
        return [SecurityViolation("FUNCTION-001", f"SQL function is not in the allowlist: {name}.")]
    for column in statement.find_all(exp.Column):
        identifier = column.this
        name = _normalize_identifier(column.name)
        if (
            not column.table
            and isinstance(identifier, exp.Identifier)
            and not identifier.args.get("quoted")
            and name in {"current_role", "user"}
        ):
            return [SecurityViolation("FUNCTION-001", f"SQL function is not in the allowlist: {name}.")]
    return []


def check_allowed_query_syntax(statement: exp.Expression) -> list[SecurityViolation]:
    """Reject query operators and clauses outside the fixed query syntax allowlist."""
    if not isinstance(statement, (exp.Select, exp.Union)):
        return [SecurityViolation("SYNTAX-001", "Only SELECT and UNION query roots are in the syntax allowlist.")]
    for union in statement.find_all(exp.Union):
        if any(union.args.get(name) for name in ("by_name", "side", "kind", "on")):
            return [SecurityViolation("SYNTAX-001", "UNION modifier is not in the syntax allowlist.")]
    forbidden = statement.find(*_FORBIDDEN_QUERY_TYPES)
    if forbidden is not None:
        return [SecurityViolation("SYNTAX-001", f"SQL syntax is not in the allowlist: {type(forbidden).__name__}.")]
    allowed_select_args = {
        "with_",
        "expressions",
        "distinct",
        "from_",
        "joins",
        "where",
        "group",
        "having",
        "order",
        "limit",
    }
    for select in statement.find_all(exp.Select):
        unsupported = next(
            (name for name, value in select.args.items() if value and name not in allowed_select_args), None
        )
        if unsupported is not None:
            return [SecurityViolation("SYNTAX-001", f"SELECT clause is not in the allowlist: {unsupported}.")]
    for predicate in statement.find_all(exp.Is):
        if not isinstance(predicate.expression, exp.Null):
            return [SecurityViolation("SYNTAX-001", "Only IS NULL and IS NOT NULL predicates are allowed.")]
    for predicate in statement.find_all(exp.Like):
        pattern = predicate.expression
        if not isinstance(pattern, exp.Literal) or not pattern.is_string:
            return [SecurityViolation("SYNTAX-001", "LIKE pattern must be a string literal.")]
        pattern_value = str(pattern.this)
        if pattern_value and not pattern_value.strip("%_"):
            return [SecurityViolation("SYNTAX-001", "LIKE pattern must contain non-wildcard text.")]
    for join in statement.find_all(exp.Join):
        if not _is_allowed_join_type(join):
            return [SecurityViolation("SYNTAX-001", "JOIN type is not in the allowlist.")]
        if str(join.args.get("kind") or "").upper() == "CROSS" and join.args.get("on") is not None:
            return [SecurityViolation("SYNTAX-001", "CROSS JOIN must not contain an ON condition.")]
        unsupported = ("global_", "hint", "match_condition", "directed", "expressions", "pivots")
        if any(join.args.get(name) for name in unsupported):
            return [SecurityViolation("SYNTAX-001", "JOIN modifier is not in the allowlist.")]
    for distinct in statement.find_all(exp.Distinct):
        if isinstance(distinct.parent, exp.Select) and (distinct.args.get("on") is not None or distinct.expressions):
            return [SecurityViolation("SYNTAX-001", "Only plain DISTINCT is in the syntax allowlist.")]
    for group in statement.find_all(exp.Group):
        if any(group.args.get(name) for name in ("grouping_sets", "cube", "rollup", "totals", "all")):
            return [SecurityViolation("SYNTAX-001", "GROUP BY modifier is not in the allowlist.")]
    for order in statement.find_all(exp.Order):
        if order.args.get("siblings"):
            return [SecurityViolation("SYNTAX-001", "ORDER SIBLINGS BY is not in the syntax allowlist.")]
    for with_clause in statement.find_all(exp.With):
        if with_clause.args.get("recursive") or with_clause.args.get("search") or with_clause.args.get("udfs"):
            return [SecurityViolation("SYNTAX-001", "WITH modifier is not in the syntax allowlist.")]
    for cte in statement.find_all(exp.CTE):
        if cte.args.get("materialized") is not None or cte.args.get("scalar") or cte.args.get("key_expressions"):
            return [SecurityViolation("SYNTAX-001", "CTE modifier is not in the syntax allowlist.")]
    for limit in statement.find_all(exp.Limit):
        if limit.args.get("offset") or limit.args.get("limit_options") or limit.args.get("expressions"):
            return [SecurityViolation("SYNTAX-001", "LIMIT modifier is not in the syntax allowlist.")]
    return []


def check_resource_usage(statement: exp.Expression) -> list[SecurityViolation]:
    """Return violations for high-confidence resource abuse patterns."""
    if sum(1 for _ in statement.walk()) > 10_000:
        return [SecurityViolation("RESOURCE-002", "SQL AST exceeds 10,000 nodes.")]
    for node in statement.find_all(exp.In):
        if len(node.expressions) > 50_000:
            return [SecurityViolation("RESOURCE-003", "IN list exceeds 50,000 expressions.")]
    violations = _check_resource_functions(statement)
    if violations:
        return violations
    violations = _check_join_shapes(statement)
    if violations:
        return violations
    for clause in (*statement.find_all(exp.Where), *statement.find_all(exp.Having)):
        if _constant_boolean(clause.this) is True:
            return [SecurityViolation("RESOURCE-008", "WHERE or HAVING condition is always true.")]
    if _returns_unfiltered_rows(statement):
        return [SecurityViolation("RESOURCE-009", "Unfiltered row query requires WHERE, HAVING, LIMIT, or FETCH.")]
    return []


def check_semantic_schema(
    statement: exp.Expression,
    *,
    dialect: str,
    schema: dict[str, Any],
) -> list[SecurityViolation]:
    """Require every source table and source column to exist in semantic metadata."""
    from dataagent.agents.nl2sql.security.checker import qualify_with_semantic_schema

    normalized_schema = {_normalize_identifier(table_name): table_meta for table_name, table_meta in schema.items()}
    allowed_tables = set(normalized_schema)
    has_base_relation = False
    for scope in traverse_scope(statement):
        for source in scope.sources.values():
            if not _is_base_relation(source):
                continue
            has_base_relation = True
            matches = _matching_schema_tables(source, allowed_tables)
            if len(matches) != 1:
                message = (
                    "Source table cannot be resolved uniquely in the provided semantic schema: "
                    f"{source.sql(dialect=dialect)}. Use exactly one modeled table and check its "
                    "table qualifier or alias."
                )
                return [SecurityViolation("SCHEMA-001", message)]
    if not has_base_relation:
        message = (
            "Query does not reference a table from the provided semantic schema. Regenerate it with at least one "
            "modeled business table and use only that table's exposed columns."
        )
        return [SecurityViolation("SCHEMA-003", message)]
    try:
        qualified = qualify_with_semantic_schema(statement, dialect=dialect, schema=schema)
    except ValueError as exc:
        message = _schema_column_resolution_message(str(exc))
        return [SecurityViolation("SCHEMA-002", message)]
    for scope in traverse_scope(qualified):
        for column in scope.columns:
            source = scope.sources.get(column.table)
            if isinstance(source, exp.Table):
                table_name = _match_schema_table(source, allowed_tables, dialect)
                allowed_columns = {
                    _normalize_identifier(column_name)
                    for column_name in normalized_schema.get(table_name, {}).get("columns", {})
                }
                if _normalize_identifier(column.name) not in allowed_columns:
                    message = _schema_column_resolution_message(column.sql())
                    return [SecurityViolation("SCHEMA-002", message)]
    return []


def _schema_column_resolution_message(detail: str) -> str:
    return (
        "Source column is missing from the provided semantic schema or cannot be resolved unambiguously: "
        f"{detail.rstrip('.')}. Ensure the column exists under the referenced table in the provided semantic schema; "
        "then check the table or CTE alias and qualify the column with its unique source."
    )


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this or "").strip('"`').lower()
    name = str(node.sql_name() or type(node).__name__).strip('"`').lower()
    if name == "exploding_generate_series":
        return "generate_series"
    if name == "pad":
        return "lpad" if node.args.get("is_left") else "rpad"
    if name == "rand":
        return "random"
    return name


def _function_namespace(node: exp.Func) -> str:
    parent = node.parent
    if isinstance(parent, exp.Dot) and parent.expression is node:
        return _normalize_identifier(parent.this.sql())
    return ""


def _check_resource_functions(statement: exp.Expression) -> list[SecurityViolation]:
    for node in statement.find_all(exp.Func):
        name = _function_name(node)
        if name in {"lpad", "rpad"}:
            arguments = list(node.iter_expressions())
            size = _pad_size(arguments)
            if size is None or size > _MAX_STRING_BYTES:
                return [SecurityViolation("RESOURCE-006", f"{name} output is unknown or exceeds 16 MiB.")]
    return []


def _check_join_shapes(statement: exp.Expression) -> list[SecurityViolation]:
    for join in statement.find_all(exp.Join):
        if not _is_allowed_join_type(join):
            continue
        if str(join.args.get("kind") or "").upper() == "CROSS" or _is_comma_join(join):
            continue
        condition = join.args.get("on")
        if condition is None and not join.args.get("using"):
            return [SecurityViolation("RESOURCE-007", "JOIN requires ON or USING unless it is a CROSS JOIN.")]
        if condition is not None and _constant_boolean(condition) is True:
            return [SecurityViolation("RESOURCE-007", "JOIN condition must not be always true.")]
    return []


def _is_comma_join(join: exp.Join) -> bool:
    # SQLGlot emits comma-separated sources without the pivots argument added by explicit JOIN parsing.
    return "pivots" not in join.args


def _is_allowed_join_type(join: exp.Join) -> bool:
    if str(join.args.get("method") or "").upper() == "NATURAL":
        return False
    side = str(join.args.get("side") or "").upper()
    kind = str(join.args.get("kind") or "").upper()
    if side:
        return side in {"LEFT", "RIGHT", "FULL"} and kind in {"", "OUTER"}
    return kind in {"", "INNER", "CROSS"}


def _returns_unfiltered_rows(statement: exp.Expression) -> bool:
    if not isinstance(statement, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
        return False
    if isinstance(statement, exp.Union):
        return _union_returns_unfiltered_rows(statement)
    scopes = traverse_scope(statement)
    if not any(any(_is_base_relation(source) for source in scope.sources.values()) for scope in scopes):
        return False
    if statement.find(exp.Where) is not None or statement.find(exp.Having) is not None:
        return False
    if statement.args.get("limit") is not None:
        return False
    if statement.find(exp.TableSample):
        return False
    if isinstance(statement, exp.Select) and (
        statement.args.get("group") is None
        and statement.find(exp.AggFunc)
        and not statement.find(exp.Window)
        and all(
            column.find_ancestor(exp.AggFunc) is not None
            for scope in traverse_scope(statement)
            for column in scope.columns
        )
    ):
        return False
    if isinstance(statement, exp.Select):
        return not all(isinstance(expression.unnest(), exp.Exists) for expression in statement.expressions)
    return True


def _union_returns_unfiltered_rows(statement: exp.Union) -> bool:
    if statement.args.get("limit") is not None or statement.find(exp.TableSample):
        return False
    scope_by_expression = {}
    for scope in traverse_scope(statement):
        scope_by_expression.update({id(scope.expression): scope})
    for branch in _union_select_branches(statement):
        scope = scope_by_expression.get(id(branch))
        if scope is None or not _scope_contains_base_relation(scope):
            continue
        if _scope_contains_row_restriction(scope) or _scope_is_single_row_aggregate(scope):
            continue
        if all(isinstance(expression.unnest(), exp.Exists) for expression in branch.expressions):
            continue
        return True
    return False


def _union_select_branches(statement: exp.Union) -> list[exp.Select]:
    branches = []
    pending = [statement.this, statement.expression]
    while pending:
        expression = pending.pop().unnest()
        if isinstance(expression, exp.Union):
            pending.extend((expression.this, expression.expression))
        elif isinstance(expression, exp.Select):
            branches.append(expression)
    return branches


def _scope_contains_base_relation(scope: Any) -> bool:
    for _, source in scope.selected_sources.values():
        if _is_base_relation(source):
            return True
        if hasattr(source, "selected_sources") and _scope_contains_base_relation(source):
            return True
    return False


def _scope_contains_row_restriction(scope: Any) -> bool:
    expression = scope.expression
    if isinstance(expression, exp.Select) and any(
        expression.args.get(name) is not None for name in ("where", "having", "limit")
    ):
        return True
    for _, source in scope.selected_sources.values():
        if hasattr(source, "selected_sources") and (
            _scope_contains_row_restriction(source) or _scope_is_single_row_aggregate(source)
        ):
            return True
    return False


def _scope_is_single_row_aggregate(scope: Any) -> bool:
    expression = scope.expression
    return bool(
        isinstance(expression, exp.Select)
        and expression.args.get("group") is None
        and expression.find(exp.AggFunc)
        and not expression.find(exp.Window)
        and all(column.find_ancestor(exp.AggFunc) is not None for column in scope.columns)
    )


def _constant_boolean(expression: exp.Expression) -> Optional[bool]:
    expression = expression.unnest()
    if isinstance(expression, exp.Boolean):
        return bool(expression.this)
    if isinstance(expression, exp.Not):
        value = _constant_boolean(expression.this)
        return None if value is None else not value
    if isinstance(expression, exp.And):
        left, right = _constant_boolean(expression.this), _constant_boolean(expression.expression)
        if left is False or right is False:
            return False
        return True if left is True and right is True else None
    if isinstance(expression, exp.Or):
        left, right = _constant_boolean(expression.this), _constant_boolean(expression.expression)
        if left is True or right is True:
            return True
        return False if left is False and right is False else None
    comparison_types = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
    if isinstance(expression, comparison_types):
        left, right = _literal_value(expression.this), _literal_value(expression.expression)
        if left is None or right is None:
            return None
        if isinstance(expression, exp.EQ):
            return left == right
        if isinstance(expression, exp.NEQ):
            return left != right
        if type(left) is not type(right):
            return None
        if isinstance(expression, exp.GT):
            return left > right
        if isinstance(expression, exp.GTE):
            return left >= right
        if isinstance(expression, exp.LT):
            return left < right
        return left <= right
    return None


def _literal_value(expression: exp.Expression) -> Optional[Any]:
    expression = expression.unnest()
    if isinstance(expression, exp.Literal):
        if expression.is_string:
            return str(expression.this)
        try:
            return Decimal(str(expression.this))
        except InvalidOperation:
            return None
    if isinstance(expression, exp.Boolean):
        return bool(expression.this)
    return None


def _numeric_literal(expression: exp.Expression) -> Optional[Decimal]:
    value = _literal_value(expression)
    return value if isinstance(value, Decimal) else None


def _pad_size(arguments: list[exp.Expression]) -> Optional[int]:
    if len(arguments) < 2:
        return None
    length = _numeric_literal(arguments[1])
    if length is None or length < 0 or length != length.to_integral_value():
        return None
    return int(length) * 4


def _match_schema_table(table: exp.Table, allowed_tables: set[str], dialect: str) -> str:
    _ = dialect
    matches = _matching_schema_tables(table, allowed_tables)
    return next(iter(matches)) if len(matches) == 1 else ""


def _is_base_relation(source: Any) -> bool:
    return isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier)


def _matching_schema_tables(table: exp.Table, allowed_tables: set[str]) -> set[str]:
    name = _normalize_identifier(table.name)
    qualifier = ".".join(
        part for part in (_normalize_identifier(table.catalog), _normalize_identifier(table.db), name) if part
    )
    if table.catalog or table.db:
        return {qualifier} & allowed_tables
    return {allowed for allowed in allowed_tables if allowed.rsplit(".", 1)[-1] == name}


def _normalize_identifier(value: str) -> str:
    return str(value or "").strip().strip('"`').lower()
