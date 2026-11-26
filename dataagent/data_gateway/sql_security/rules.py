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

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlglot import Tokenizer, exp
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.tokens import TokenType

from dataagent.data_gateway.sql_security.models import SecurityViolation
from dataagent.data_gateway.sql_security.qualify import qualify_with_semantic_schema

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
# SQLGlot 28 represents these context functions as bare columns.
_COLUMN_CONTEXT_FUNCTION_NAMES = frozenset(
    {"current_catalog", "current_database", "current_path", "current_role", "session_user", "user"}
)


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
_GROUPING_MODIFIER_TYPES = _resolve_expression_types(("Rollup", "Cube", "GroupingSets"))


def check_allowed_functions(
    statement: exp.Expression,
    *,
    sql: str,
    dialect: str,
) -> list[SecurityViolation]:
    """Require every SQL function to use an explicitly allowed source spelling."""
    # SQLGlot normalizes equivalent functions in the AST, so inspect source tokens to preserve names such as
    # SUBSTR versus SUBSTRING and EXTRACT versus DATE_PART.
    # A CTE name followed by a column list looks like a call in the token stream.
    cte_name_spans = {
        (alias.this.meta.get("start"), alias.this.meta.get("end"))
        for cte in statement.find_all(exp.CTE)
        if isinstance(alias := cte.args.get("alias"), exp.TableAlias)
        and isinstance(alias.this, exp.Identifier)
        and alias.columns
    }
    tokens = Tokenizer(dialect=dialect).tokenize(sql)
    for index, token in enumerate(tokens[:-1]):
        if tokens[index + 1].token_type is not TokenType.L_PAREN:
            continue
        if token.token_type not in {TokenType.IDENTIFIER, TokenType.VAR}:
            continue
        if (token.start, token.end) in cte_name_spans:
            continue
        name = _normalize_identifier(token.text)
        quoted = token.token_type is TokenType.IDENTIFIER
        qualified = index > 0 and tokens[index - 1].token_type is TokenType.DOT
        if quoted or qualified or name not in _ALLOWED_FUNCTIONS:
            return [
                SecurityViolation("FUNCTION-001", f"SQL function is not in the allowlist: {name}.")
            ]
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
            return [
                SecurityViolation(
                    "FUNCTION-001", f"SQL function is not in the allowlist: {qualified_name}."
                )
            ]
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
            and name in _COLUMN_CONTEXT_FUNCTION_NAMES
        ):
            return [
                SecurityViolation("FUNCTION-001", f"SQL function is not in the allowlist: {name}.")
            ]
    return []


def check_allowed_query_syntax(
    statement: exp.Expression,
    *,
    nl2sql_policy: bool = False,
    grouping_statement: Callable[[], exp.Expression] | None = None,
) -> list[SecurityViolation]:
    """Reject query operators and clauses outside the fixed query syntax allowlist."""
    if not isinstance(statement, (exp.Select, exp.Union)):
        return [
            SecurityViolation(
                "SYNTAX-001", "Only SELECT and UNION query roots are in the syntax allowlist."
            )
        ]
    for union in statement.find_all(exp.Union):
        if any(union.args.get(name) for name in ("by_name", "side", "kind", "on")):
            return [
                SecurityViolation("SYNTAX-001", "UNION modifier is not in the syntax allowlist.")
            ]
    forbidden = statement.find(*_FORBIDDEN_QUERY_TYPES)
    if forbidden is not None:
        return [
            SecurityViolation(
                "SYNTAX-001", f"SQL syntax is not in the allowlist: {type(forbidden).__name__}."
            )
        ]
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
            (
                name
                for name, value in select.args.items()
                if value and name not in allowed_select_args
            ),
            None,
        )
        if unsupported is not None:
            return [
                SecurityViolation(
                    "SYNTAX-001", f"SELECT clause is not in the allowlist: {unsupported}."
                )
            ]
    for predicate in statement.find_all(exp.Is):
        if not isinstance(predicate.expression, exp.Null):
            return [
                SecurityViolation(
                    "SYNTAX-001", "Only IS NULL and IS NOT NULL predicates are allowed."
                )
            ]
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
        if isinstance(distinct.parent, exp.Select) and (
            distinct.args.get("on") is not None or distinct.expressions
        ):
            return [
                SecurityViolation("SYNTAX-001", "Only plain DISTINCT is in the syntax allowlist.")
            ]
    for group in statement.find_all(exp.Group):
        if any(
            group.args.get(name) for name in ("grouping_sets", "cube", "rollup", "totals", "all")
        ) or (
            nl2sql_policy
            and any(isinstance(item, _GROUPING_MODIFIER_TYPES) for item in group.expressions)
        ):
            return [SecurityViolation("SYNTAX-001", "GROUP BY modifier is not in the allowlist.")]
    for order in statement.find_all(exp.Order):
        if order.args.get("siblings"):
            return [
                SecurityViolation("SYNTAX-001", "ORDER SIBLINGS BY is not in the syntax allowlist.")
            ]
    for with_clause in statement.find_all(exp.With):
        if (
            with_clause.args.get("recursive")
            or with_clause.args.get("search")
            or with_clause.args.get("udfs")
        ):
            return [
                SecurityViolation("SYNTAX-001", "WITH modifier is not in the syntax allowlist.")
            ]
    for cte in statement.find_all(exp.CTE):
        if (
            cte.args.get("materialized") is not None
            or cte.args.get("scalar")
            or cte.args.get("key_expressions")
        ):
            return [SecurityViolation("SYNTAX-001", "CTE modifier is not in the syntax allowlist.")]
    for limit in statement.find_all(exp.Limit):
        if (
            limit.args.get("offset")
            or limit.args.get("limit_options")
            or limit.args.get("expressions")
        ):
            return [
                SecurityViolation("SYNTAX-001", "LIMIT modifier is not in the syntax allowlist.")
            ]
    if nl2sql_policy:
        return _check_grouped_projections(grouping_statement() if grouping_statement else statement)
    return []


def resolve_grouping_aliases(
    statement: exp.Expression, *, dialect: str, schema: dict[str, Any]
) -> exp.Expression:
    """Bind unqualified GROUP BY aliases for the NL2SQL grouping checks."""
    selects = list(statement.find_all(exp.Select))
    candidates = {}
    for index, select in enumerate(selects):
        group = select.args.get("group")
        if not isinstance(group, exp.Group):
            continue
        aliases: dict[str, list[int]] = {}
        for position, projection in enumerate(select.expressions):
            if isinstance(projection, exp.Alias):
                aliases.setdefault(projection.alias.casefold(), []).append(position)
        references = []
        for item_index, item in enumerate(group.expressions):
            if isinstance(item, exp.Column) and not item.table:
                positions = aliases.get(item.name.casefold(), [])
                if len(positions) == 1:
                    references.append((item_index, positions[0]))
        if references:
            candidates[index] = references
    if not candidates:
        return statement

    try:
        qualified = qualify_with_semantic_schema(statement, dialect=dialect, schema=schema)
    except ValueError:
        # The workflow leaves source-column validation to the database.
        return statement
    qualified_selects = list(qualified.find_all(exp.Select))
    if len(selects) != len(qualified_selects):
        return statement

    analysis = statement.copy()
    analysis_selects = list(analysis.find_all(exp.Select))
    for index, references in candidates.items():
        select = qualified_selects[index]
        resolved_group = select.args.get("group")
        if not isinstance(resolved_group, exp.Group):
            continue
        group = analysis_selects[index].args["group"]
        if len(resolved_group.expressions) != len(group.expressions) or len(
            select.expressions
        ) != len(analysis_selects[index].expressions):
            continue
        for item_index, position in references:
            projection = select.expressions[position]
            resolved = resolved_group.expressions[item_index].unnest()
            if resolved == projection.unalias().unnest():
                # A conflicting source column would resolve to a different expression.
                group.expressions[item_index].replace(exp.Literal.number(position + 1))
    return analysis


def _check_grouped_projections(statement: exp.Expression) -> list[SecurityViolation]:
    """Reject a selected column that GROUP BY leaves neither grouped nor aggregated."""
    violations = []
    for select in statement.find_all(exp.Select):
        missing = _ungrouped_projection_columns(select)
        if missing:
            rendered = ", ".join(f"`{name}`" for name in missing)
            violations.append(
                SecurityViolation(
                    "SYNTAX-001",
                    (
                        f"Selected column {rendered} is not in GROUP BY and is not aggregated, "
                        "so the query cannot execute. Wrap a metric in SUM(), or add a dimension to GROUP BY."
                    ),
                )
            )
    return violations


def check_resource_usage(
    statement: exp.Expression,
    *,
    nl2sql_policy: bool = False,
    grouping_statement: Callable[[], exp.Expression] | None = None,
) -> list[SecurityViolation]:
    """Return violations for high-confidence resource abuse patterns."""
    if sum(1 for _ in statement.walk()) > 10_000:
        return [SecurityViolation("RESOURCE-002", "SQL AST exceeds 10,000 nodes.")]
    for node in statement.find_all(exp.In):
        if len(node.expressions) > 50_000:
            return [SecurityViolation("RESOURCE-003", "IN list exceeds 50,000 expressions.")]
    violations = _check_resource_functions(statement)
    if violations:
        return violations
    violations = _check_join_shapes(statement, nl2sql_policy=nl2sql_policy)
    if violations:
        return violations
    for clause in (*statement.find_all(exp.Where), *statement.find_all(exp.Having)):
        if _constant_boolean(clause.this) is True:
            return [SecurityViolation("RESOURCE-008", "WHERE or HAVING condition is always true.")]
    if _returns_unfiltered_rows(
        grouping_statement() if grouping_statement else statement, nl2sql_policy=nl2sql_policy
    ):
        return [
            SecurityViolation(
                "RESOURCE-009", "Unfiltered row query requires WHERE, HAVING, LIMIT, or FETCH."
            )
        ]
    return []


def check_semantic_schema(
    statement: exp.Expression,
    *,
    dialect: str,
    schema: dict[str, Any],
    validate_columns: bool = True,
) -> list[SecurityViolation]:
    """Require modeled source tables and, when enabled, their exposed columns."""
    normalized_schema = {
        _normalize_identifier(table_name): table_meta for table_name, table_meta in schema.items()
    }
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
    if not validate_columns:
        return []
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
                return [
                    SecurityViolation(
                        "RESOURCE-006", f"{name} output is unknown or exceeds 16 MiB."
                    )
                ]
    return []


def _check_join_shapes(
    statement: exp.Expression, *, nl2sql_policy: bool = False
) -> list[SecurityViolation]:
    for join in statement.find_all(exp.Join):
        if not _is_allowed_join_type(join):
            continue
        if str(join.args.get("kind") or "").upper() == "CROSS" or _is_comma_join(join):
            continue
        condition = join.args.get("on")
        if condition is None and not join.args.get("using"):
            return [
                SecurityViolation(
                    "RESOURCE-007", "JOIN requires ON or USING unless it is a CROSS JOIN."
                )
            ]
        if condition is not None and _constant_boolean(condition) is True:
            message = "JOIN condition must not be always true."
            if nl2sql_policy:
                if _join_has_single_row_source(statement, join):
                    continue
                message += " Use CROSS JOIN when a Cartesian product is intended."
            return [SecurityViolation("RESOURCE-007", message)]
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


def _returns_unfiltered_rows(statement: exp.Expression, *, nl2sql_policy: bool) -> bool:
    if not isinstance(statement, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
        return False
    if isinstance(statement, exp.Union):
        return _union_returns_unfiltered_rows(statement, nl2sql_policy=nl2sql_policy)
    scopes = list(traverse_scope(statement))
    if not any(
        any(_is_base_relation(source) for source in scope.sources.values()) for scope in scopes
    ):
        return False
    if nl2sql_policy:
        root = next((scope for scope in scopes if scope.expression is statement), None)
        if root is not None:
            return not _scope_output_is_restricted(root)
        return not any(
            statement.args.get(name) is not None for name in ("where", "having", "limit")
        )
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
        return not all(
            isinstance(expression.unnest(), exp.Exists) for expression in statement.expressions
        )
    return True


def _union_returns_unfiltered_rows(statement: exp.Union, *, nl2sql_policy: bool) -> bool:
    if statement.args.get("limit") is not None or statement.find(exp.TableSample):
        return False
    scope_by_expression = {}
    for scope in traverse_scope(statement):
        scope_by_expression.update({id(scope.expression): scope})
    for branch in _union_select_branches(statement):
        scope = scope_by_expression.get(id(branch))
        if scope is None or not _scope_contains_base_relation(scope):
            continue
        if nl2sql_policy:
            restricted = _scope_output_is_restricted(scope)
        else:
            restricted = (
                _scope_contains_row_restriction(scope)
                or _scope_is_single_row_aggregate(scope)
                or all(isinstance(item.unnest(), exp.Exists) for item in branch.expressions)
            )
        if not restricted:
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


def _is_grouped_aggregate(statement: exp.Select) -> bool:
    """A GROUP BY aggregate collapses rows, so it is not an unfiltered row dump."""
    group = statement.args.get("group")
    if (
        not isinstance(group, exp.Group)
        or statement.find(exp.Window)
        or not statement.find(exp.AggFunc)
    ):
        return False
    return any(
        aggregate.find_ancestor(exp.Select) is statement
        for projection in statement.expressions
        for aggregate in projection.find_all(exp.AggFunc)
    ) and not _ungrouped_projection_columns(statement)


def _ungrouped_projection_columns(select: exp.Select) -> list[str]:
    group = select.args.get("group")
    if not isinstance(group, exp.Group):
        return []
    projections = select.expressions
    grouped_names = set()
    keys = set()
    for item in group.expressions:
        item = item.unnest()
        if item.is_int:
            digits = item.name.lstrip("0")
            position = int(digits) if digits and len(digits) <= len(str(len(projections))) else 0
            if 1 <= position <= len(projections) and not any(
                projection.unalias().is_star for projection in projections[:position]
            ):
                keys.add(projections[position - 1].unalias().unnest())
        else:
            grouped_names.update(
                column.name.casefold()
                for column in item.find_all(exp.Column)
                if column.find_ancestor(exp.Select) is select
            )

    missing: dict[str, str] = {}
    for projection in projections:
        for column in projection.walk(prune=lambda expression: expression.unnest() in keys):
            if (
                isinstance(column, exp.Column)
                and column.find_ancestor(exp.Select) is select
                and column not in keys
                and column.name.casefold() not in grouped_names
                and not _column_is_aggregated(column, select)
            ):
                missing.setdefault(column.name.casefold(), column.name)
    return list(missing.values())


def _column_is_aggregated(node: exp.Expression, select: exp.Select) -> bool:
    current = node.parent
    while current is not None and current is not select:
        if isinstance(current, exp.Select):
            return False
        if isinstance(current, exp.AggFunc):
            return True
        if isinstance(current, exp.Filter) and isinstance(current.this, exp.AggFunc):
            return True
        current = current.parent
    return False


def _scope_output_is_restricted(
    scope: Any,
    *,
    cache: dict[int, bool] | None = None,
    stack: set[int] | None = None,
) -> bool:
    """Whether this scope's output rows are filtered, limited, or collapsed.

    A WHERE inside a scalar subquery or a joined subquery does not filter a base
    table read in the same scope. A derived table or CTE counts only when its own
    output is restricted, or when this scope adds WHERE, HAVING, or LIMIT.
    """
    if cache is None:
        cache = {}
    if stack is None:
        stack = set()
    marker = id(scope)
    cached = cache.get(marker)
    if cached is not None:
        return cached
    if marker in stack:
        return False
    stack.add(marker)
    restricted = _scope_output_restriction(scope, cache=cache, stack=stack)
    stack.remove(marker)
    cache[marker] = restricted
    return restricted


def _scope_output_restriction(scope: Any, *, cache: dict[int, bool], stack: set[int]) -> bool:
    expression = getattr(scope, "expression", None)
    if isinstance(expression, exp.Select):
        if all(isinstance(item.unnest(), exp.Exists) for item in expression.expressions):
            return True
        if _is_grouped_aggregate(expression) or _scope_projection_is_single_row_aggregate(scope):
            return True
        if any(expression.args.get(name) is not None for name in ("where", "having", "limit")):
            return True
    direct_base = False
    nested_sources: list[Any] = []
    for source in getattr(scope, "sources", {}).values():
        if _is_base_relation(source):
            direct_base = True
            if isinstance(source, exp.Table) and source.args.get("sample") is not None:
                return True
        elif hasattr(source, "sources"):
            nested_sources.append(source)
    if direct_base:
        return False
    if not nested_sources:
        return True
    return all(
        _scope_output_is_restricted(source, cache=cache, stack=stack) for source in nested_sources
    )


def _scope_projection_is_single_row_aggregate(scope: Any) -> bool:
    expression = scope.expression
    if (
        not isinstance(expression, exp.Select)
        or expression.args.get("group") is not None
        or expression.find(exp.Window)
    ):
        return False
    has_aggregate = False
    for projection in expression.expressions:
        for function in projection.find_all(exp.Func):
            if (
                function.find_ancestor(exp.Select) is expression
                and _function_name(function) == "generate_series"
            ):
                return False
        for aggregate in projection.find_all(exp.AggFunc):
            if aggregate.find_ancestor(exp.Select) is expression:
                has_aggregate = True
        for column in projection.find_all(exp.Column, exp.Star):
            if column.find_ancestor(exp.Select) is not expression:
                continue
            aggregate_filter = column.find_ancestor(exp.Filter)
            if column.find_ancestor(exp.AggFunc) is None and not (
                aggregate_filter is not None and isinstance(aggregate_filter.this, exp.AggFunc)
            ):
                return False
    return has_aggregate


def _join_has_single_row_source(statement: exp.Expression, join: exp.Join) -> bool:
    select = join.find_ancestor(exp.Select)
    if select is None or len(select.args.get("joins") or []) != 1:
        return False
    from_clause = select.args.get("from_")
    if not isinstance(from_clause, exp.From) or from_clause.this is None:
        return False
    scope = next((item for item in traverse_scope(statement) if item.expression is select), None)
    if scope is None:
        return False
    left_source = scope.sources.get(from_clause.this.alias_or_name)
    right_source = scope.sources.get(join.this.alias_or_name)
    left_is_single_row = hasattr(
        left_source, "selected_sources"
    ) and _scope_projection_is_single_row_aggregate(left_source)
    right_is_single_row = hasattr(
        right_source, "selected_sources"
    ) and _scope_projection_is_single_row_aggregate(right_source)
    return left_is_single_row or right_is_single_row


def _constant_boolean(expression: exp.Expression) -> Optional[bool]:
    comparison_types = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
    stack: list[exp.Expression] = [expression.unnest()]
    values: dict[int, Optional[bool]] = {}

    while stack:
        node = stack[-1]
        node_id = id(node)
        if node_id in values:
            stack.pop()
            continue
        if isinstance(node, exp.Boolean):
            values[node_id] = bool(node.this)
            stack.pop()
            continue
        if isinstance(node, exp.Not):
            child = node.this.unnest()
            if id(child) not in values:
                stack.append(child)
                continue
            child_value = values[id(child)]
            values[node_id] = None if child_value is None else (not child_value)
            stack.pop()
            continue
        if isinstance(node, (exp.And, exp.Or)):
            left = node.this.unnest()
            right = node.expression.unnest()
            if id(left) not in values:
                stack.append(left)
                continue
            if id(right) not in values:
                stack.append(right)
                continue
            left_value, right_value = values[id(left)], values[id(right)]
            if isinstance(node, exp.And):
                if left_value is False or right_value is False:
                    values[node_id] = False
                elif left_value is True and right_value is True:
                    values[node_id] = True
                else:
                    values[node_id] = None
            elif left_value is True or right_value is True:
                values[node_id] = True
            elif left_value is False and right_value is False:
                values[node_id] = False
            else:
                values[node_id] = None
            stack.pop()
            continue
        if isinstance(node, comparison_types):
            left, right = _literal_value(node.this), _literal_value(node.expression)
            if left is None or right is None:
                values[node_id] = None
            elif isinstance(node, exp.EQ):
                values[node_id] = left == right
            elif isinstance(node, exp.NEQ):
                values[node_id] = left != right
            elif type(left) is not type(right):
                values[node_id] = None
            elif isinstance(node, exp.GT):
                values[node_id] = left > right
            elif isinstance(node, exp.GTE):
                values[node_id] = left >= right
            elif isinstance(node, exp.LT):
                values[node_id] = left < right
            else:
                values[node_id] = left <= right
            stack.pop()
            continue
        values[node_id] = None
        stack.pop()

    return values[id(expression.unnest())]


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
        part
        for part in (_normalize_identifier(table.catalog), _normalize_identifier(table.db), name)
        if part
    )
    if table.catalog or table.db:
        return {qualifier} & allowed_tables
    return {allowed for allowed in allowed_tables if allowed.rsplit(".", 1)[-1] == name}


def _normalize_identifier(value: str) -> str:
    return str(value or "").strip().strip('"`').lower()
