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
"""Resolve SQL identifiers that collide with keyword-shaped functions."""

# ruff: noqa: UP045

from typing import Any, Optional, cast

import sqlglot
from sqlglot import Tokenizer, TokenType, exp
from sqlglot.errors import ErrorLevel, ParseError
from sqlglot.expressions.core import Expression
from sqlglot.optimizer.scope import traverse_scope

_COLUMN_CONTEXT_FUNCTION_NAMES = frozenset({"current_role", "user"})


def normalize_semantic_column_references(
    statement: Expression,
    *,
    sql: str,
    dialect: str,
    schema: dict[str, Any],
) -> Optional[str]:
    """Quote bare keyword-shaped names only when semantic metadata resolves them as columns."""
    from dataagent.agents.nl2sql.security.checker import qualify_with_semantic_schema

    normalized_sql = sql
    for name in sorted(_candidate_names(statement)):
        candidate_sql = _quote_candidate_tokens(normalized_sql, dialect, name)
        if candidate_sql == normalized_sql:
            continue
        try:
            candidate = cast(Expression, sqlglot.parse_one(candidate_sql, read=dialect, error_level=ErrorLevel.RAISE))
            qualify_with_semantic_schema(candidate, dialect=dialect, schema=schema)
        except ParseError:
            continue
        except ValueError:
            if not _candidate_is_unique_derived_column(statement, name):
                continue
        normalized_sql = candidate_sql
    return normalized_sql if normalized_sql != sql else None


def _candidate_names(statement: Expression) -> set[str]:
    names = {
        _function_name(node)
        for node in statement.find_all(exp.Func)
        if node.meta.get("start") is None and not node.args
    }
    for column in statement.find_all(exp.Column):
        identifier = column.this
        name = _normalize_identifier(column.name)
        if (
            not column.table
            and name in _COLUMN_CONTEXT_FUNCTION_NAMES
            and isinstance(identifier, exp.Identifier)
            and not identifier.args.get("quoted")
        ):
            names.add(name)
    return names


def _candidate_is_unique_derived_column(statement: Expression, name: str) -> bool:
    found = False
    for scope in traverse_scope(statement):
        expression = scope.expression
        if not isinstance(expression, exp.Select) or not _select_has_function_candidate(expression, name):
            continue
        found = True
        matching_sources = 0
        for _, source in scope.selected_sources.values():
            if not hasattr(source, "selected_sources"):
                continue
            outputs = {_normalize_identifier(output) for output in source.expression.named_selects}
            if name in outputs:
                matching_sources += 1
        if matching_sources != 1:
            return False
    return found


def _select_has_function_candidate(select: exp.Select, name: str) -> bool:
    for node in select.find_all(exp.Func):
        if node.find_ancestor(exp.Select) is select and _function_name(node) == name and not node.args:
            return True
    return False


def _quote_candidate_tokens(sql: str, dialect: str, candidate_name: str) -> str:
    tokens = Tokenizer(dialect=dialect).tokenize(sql)
    edits = []
    for index, token in enumerate(tokens):
        previous = tokens[index - 1] if index else None
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if _normalize_identifier(token.text) != candidate_name:
            continue
        if token.token_type in {TokenType.IDENTIFIER, TokenType.STRING}:
            continue
        if previous is not None and previous.token_type is TokenType.DOT:
            continue
        if following is not None and following.token_type is TokenType.L_PAREN:
            continue
        edits.append((token.start, token.end + 1, f'"{candidate_name}"'))

    normalized_sql = sql
    for start, end, replacement in reversed(edits):
        normalized_sql = f"{normalized_sql[:start]}{replacement}{normalized_sql[end:]}"
    return normalized_sql


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return _normalize_identifier(node.this)
    return _normalize_identifier(node.sql_name() or type(node).__name__)


def _normalize_identifier(value: Any) -> str:
    return str(value or "").strip().strip('"`').lower()
