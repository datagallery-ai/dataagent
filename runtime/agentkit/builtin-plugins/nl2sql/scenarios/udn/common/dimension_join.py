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
"""Dimension-join machinery shared by the UDN scenario packs.

Ported from the legacy ``nl2sql_utils`` dimension functions. Columns that map
to a dimension table and are projected standalone in the final SELECT are
rewritten into dimension value lookups (one ``INNER JOIN`` per mapping); the
dimension tables' DDL and join rules are injected into the run state so the
validator can re-check the rewritten SQL.

``dimension_join_rewriter`` wraps the machinery into the ``post_sql`` handler
shape. The owning node's failure contract (keep the originals and continue)
applies when the handler itself raises; per-candidate failures only flag
``need_ref`` for another reflection round.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import sqlglot
from langchain_core.language_models import BaseChatModel
from loguru import logger
from nl2sql.hooks import PostSqlHandler
from nl2sql.state import Result
from nl2sql.utils.nl2sql_utils import schema_to_ddl, sql_parser
from sqlglot import exp

from .llm import execute_with_llm

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def load_dimensions(path: Path, scenario: str) -> dict[str, dict[str, str]]:
    """Load and validate one pack's packaged fact-to-dimension mappings."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        dimensions = payload["dimensions"]
        required = {"dimension_table", "key_column", "value_column"}
        if payload.get("scenario") != scenario or not isinstance(dimensions, dict):
            raise ValueError("scenario or dimensions is invalid")
        if any(not isinstance(item, dict) or not required.issubset(item) for item in dimensions.values()):
            raise ValueError("dimension mapping fields are incomplete")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid dimension config: {path}: {exc}") from exc
    return dimensions


def selected_dimensions(sql: str, mappings: dict[str, dict[str, str]], dialect: str) -> list[str]:
    """Return mapped standalone columns from the final SELECT projection."""
    statement = sqlglot.parse_one(sql, read=dialect)
    if not isinstance(statement, exp.Select):
        return []
    selected = []
    for projection in statement.expressions:
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        expression = expression.unnest()
        if isinstance(expression, exp.Column) and expression.name in mappings:
            selected.append(expression.name)
    return list(dict.fromkeys(selected))


def _dimension_rule_line(name: str, mapping: dict[str, str]) -> str:
    table = mapping["dimension_table"]
    key = mapping["key_column"]
    value = mapping["value_column"]
    return (
        f"- When `{name}` is projected, use `{table}.{value}` through "
        f"`INNER JOIN {table} ON <fact>.{name} = {table}.{key}`. "
        "This is a required display-value lookup, not extra business logic; "
        "WHERE/HAVING predicates remain on the fact key."
    )


def _add_dimension_context(state: dict[str, Any], dimensions: list[str], mappings: dict[str, dict[str, str]]) -> str:
    fact_table = next(iter(state["schema"]), "")
    schema_str = state["schema_str"] or ""
    sql_rules = state["sql_rules"] or ""
    new_tables: dict[str, dict[str, Any]] = {}
    new_rule_lines: list[str] = []
    for name in dimensions:
        mapping = mappings[name]
        table = mapping["dimension_table"]
        key = mapping["key_column"]
        value = mapping["value_column"]
        table_meta = state["schema"].setdefault(
            table,
            {"description": "Dimension value lookup table", "columns": {}},
        )
        for column in (key, value):
            table_meta["columns"].setdefault(column, {"description": "", "value_type": ""})
        if fact_table:
            join = (f"{fact_table}.{name}", f"{table}.{key}")
            if join not in state["joins"]:
                state["joins"].append(join)
        if f"CREATE TABLE `{table}`" not in schema_str:
            new_tables[table] = table_meta
        rule_line = _dimension_rule_line(name, mapping)
        if rule_line not in sql_rules:
            new_rule_lines.append(rule_line)
    parts: list[str] = []
    if new_tables:
        schema_ddl = schema_to_ddl(new_tables)
        schema_block = (
            schema_ddl if "## Dimension Lookup Schema" in schema_str else f"## Dimension Lookup Schema\n{schema_ddl}"
        )
        state["schema_str"] = f"{schema_str}\n\n{schema_block}".strip()
        parts.append(schema_block)
    if new_rule_lines:
        rules_body = "\n".join(new_rule_lines)
        rules_block = (
            rules_body
            if "## Required Dimension Value Mappings" in sql_rules
            else f"## Required Dimension Value Mappings\n{rules_body}"
        )
        state["sql_rules"] = f"{sql_rules}\n\n{rules_block}".strip()
        parts.append(rules_block)
    return "\n\n".join(parts)


async def _rewrite_candidate(
    result: Result,
    mappings: dict[str, dict[str, str]],
    dialect: str,
    model: BaseChatModel,
) -> list[str]:
    """Rewrite one candidate in place; returns the dimensions it projected."""
    try:
        dimensions = selected_dimensions(result.sql, mappings, dialect)
    except sqlglot.errors.SqlglotError as exc:
        logger.warning(f"Dimension detection failed: {exc}")
        result.need_ref = True
        return []
    if not dimensions:
        result.need_ref = False
        return []
    context = {
        "dialect": dialect,
        "sql": result.sql,
        "mappings": json.dumps(
            {name: mappings[name] for name in dimensions},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    try:
        rewritten = sql_parser(await execute_with_llm(model, _PROMPT_DIR, "dimension_join_", context))[-1]
        sqlglot.parse_one(rewritten, read=dialect)
    except Exception as exc:
        logger.warning(f"Dimension rewrite failed: {exc}")
        result.need_ref = True
        return dimensions
    result.sql = rewritten
    result.need_ref = False
    return dimensions


def dimension_join_rewriter(
    mappings: dict[str, dict[str, str]],
    model: BaseChatModel,
    dialect: str,
) -> PostSqlHandler:
    """Build a ``post_sql`` handler that rewrites projected dimensions into lookups."""

    async def rewrite(results: list[Result], state: dict[str, Any]) -> list[Result]:
        dimension_lists = await asyncio.gather(
            *(_rewrite_candidate(result, mappings, dialect, model) for result in results)
        )
        dimensions = list(dict.fromkeys(name for names in dimension_lists for name in names))
        dimension_context = _add_dimension_context(state, dimensions, mappings) if dimensions else ""
        if dimension_context:
            for result in results:
                result.prompt = f"{result.prompt}\n\n{dimension_context}"
        return results

    return rewrite
