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

The rewrite is deterministic first: a SQLGlot rewriter swaps each projected
(or GROUP BY-only) fact dimension for its lookup-table value, syncs the
clause references, and adds the INNER JOIN. An atomic postcondition check
verifies the result; a failed or unsafe shape falls back to the LLM rewrite,
whose output must pass the same postconditions before it is accepted.
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
    return _projected_dimension_columns(statement, mappings)


def _projected_dimension_columns(statement: exp.Select, mappings: dict[str, dict[str, str]]) -> list[str]:
    """Return mapped fact dimension names among the statement's standalone projections."""
    fact = _from_table(statement)
    source_tables = _source_tables(statement)
    selected = []
    for projection in statement.expressions:
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        expression = expression.unnest()
        if (
            isinstance(expression, exp.Column)
            and expression.name in mappings
            and _reads_fact_table(expression, fact, source_tables)
        ):
            selected.append(expression.name)
    return list(dict.fromkeys(selected))


def _grouped_dimension_columns(
    statement: exp.Select,
    mappings: dict[str, dict[str, str]],
    skip: set[str],
    source_tables: list[exp.Table],
) -> list[str]:
    """Return mapped fact dimensions referenced in GROUP BY but not projected.

    ORDER BY-only names deliberately do not count: backfilling them would
    silently grow the result set and add a row-losing INNER JOIN, and an
    aggregate query ordering by an ungrouped column is invalid SQL anyway.
    """
    joined = {table.name for table in source_tables}
    fact = _from_table(statement)
    found: list[str] = []
    group = statement.args.get("group")
    if group is None:
        return found
    for expression in group.expressions:
        if not isinstance(expression, exp.Column) or expression.name not in mappings:
            continue
        if not _reads_fact_table(expression, fact, source_tables):
            continue
        name = expression.name
        if name in skip or name in found or mappings[name]["dimension_table"] in joined:
            continue
        found.append(name)
    return found


def _from_table(statement: exp.Select) -> exp.Table | None:
    """Return the plain FROM table, or None when FROM is missing or a subquery."""
    clause = statement.args.get("from_") or statement.args.get("from")
    table = clause.this if clause is not None else None
    return table if isinstance(table, exp.Table) else None


def _source_tables(statement: exp.Select) -> list[exp.Table]:
    """Return every plain table the statement reads from (FROM plus joins)."""
    tables: list[exp.Table] = []
    from_table = _from_table(statement)
    if from_table is not None:
        tables.append(from_table)
    for join in statement.args.get("joins") or []:
        if isinstance(join.this, exp.Table):
            tables.append(join.this)
    return tables


def _reads_fact_table(column: exp.Column, fact: exp.Table | None, source_tables: list[exp.Table]) -> bool:
    """Return True when the column plausibly reads the fact table.

    Bare columns, the fact table's own qualifier, and unknown qualifiers (e.g.
    a subquery alias) may all be fact references; a qualifier naming another
    joined source table marks the column as that table's instead.
    """
    if not column.table:
        return True
    if fact is not None and column.table == fact.alias_or_name:
        return True
    return column.table not in {table.alias_or_name for table in source_tables}


def _has_ordinal_reference(statement: exp.Select) -> bool:
    """Return True when GROUP BY / ORDER BY reference output positions (``GROUP BY 1``)."""
    group = statement.args.get("group")
    if group is not None and any(isinstance(e, exp.Literal) and e.is_int for e in group.expressions):
        return True
    order = statement.args.get("order")
    if order is not None:
        for expression in order.expressions:
            inner = expression.this if isinstance(expression, exp.Ordered) else expression
            if isinstance(inner, exp.Literal) and inner.is_int:
                return True
    return False


def _replace_or_backfill_projection(statement: exp.Select, name: str, value_column: str, alias: str) -> exp.Select:
    """Swap the standalone ``name`` projection for ``<alias>.<value> AS name``."""
    fact = _from_table(statement)
    source_tables = _source_tables(statement)
    rebuilt = []
    replaced = False
    for projection in statement.expressions:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(inner, exp.Column) and inner.name == name and _reads_fact_table(inner, fact, source_tables):
            output_name = projection.alias if isinstance(projection, exp.Alias) else name
            rebuilt.append(exp.alias_(exp.column(value_column, table=alias), output_name))
            replaced = True
        else:
            rebuilt.append(projection)
    if not replaced:
        insert_at = len(rebuilt)
        for index, projection in enumerate(rebuilt):
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(inner, exp.Column):
                insert_at = index + 1
        rebuilt.insert(insert_at, exp.alias_(exp.column(value_column, table=alias), name))
    statement.set("expressions", rebuilt)
    return statement


def _sync_clause_columns(statement: exp.Select, clause: str, name: str, value_column: str, alias: str) -> None:
    """Point GROUP BY / ORDER BY references of ``name`` at the lookup value column."""
    node = statement.args.get(clause)
    if node is None:
        return
    fact = _from_table(statement)
    source_tables = _source_tables(statement)
    updated = []
    for expression in node.expressions:
        if isinstance(expression, exp.Ordered):
            if (
                isinstance(expression.this, exp.Column)
                and expression.this.name == name
                and _reads_fact_table(expression.this, fact, source_tables)
            ):
                expression.set("this", exp.column(value_column, table=alias))
        elif (
            isinstance(expression, exp.Column)
            and expression.name == name
            and _reads_fact_table(expression, fact, source_tables)
        ):
            expression = exp.column(value_column, table=alias)
        updated.append(expression)
    node.set("expressions", updated)


def _rewrite_dimensions_with_sqlglot(
    statement: exp.Select,
    dimensions: list[str],
    mappings: dict[str, dict[str, str]],
    dialect: str,
) -> tuple[str, list[str]] | None:
    """Deterministically rewrite projected dimensions onto their lookup joins."""
    fact = _from_table(statement)
    if fact is None or _has_ordinal_reference(statement):
        return None
    source_tables = _source_tables(statement)
    source_names = {table.name for table in source_tables} | {table.alias_or_name for table in source_tables}
    todo = [name for name in dimensions if mappings[name]["dimension_table"] not in source_names]
    if not todo:
        return None
    out = statement.copy()
    fact_name = fact.alias_or_name
    used_names = set(source_names)
    for name in todo:
        mapping = mappings[name]
        table = mapping["dimension_table"]
        alias = table
        suffix = 2
        while alias in used_names:
            alias = f"{table}_{suffix}"
            suffix += 1
        used_names.add(alias)
        out = _replace_or_backfill_projection(out, name, mapping["value_column"], alias)
        _sync_clause_columns(out, "group", name, mapping["value_column"], alias)
        _sync_clause_columns(out, "order", name, mapping["value_column"], alias)
        join_table = (
            exp.Table(this=exp.to_identifier(table))
            if alias == table
            else exp.Table(this=exp.to_identifier(table), alias=exp.TableAlias(this=exp.to_identifier(alias)))
        )
        out = out.join(
            join_table,
            on=exp.EQ(
                this=exp.column(name, table=fact_name),
                expression=exp.column(mapping["key_column"], table=alias),
            ),
            join_type="inner",
        )
        for column in out.find_all(exp.Column):
            if column.table or column.name != name:
                continue
            if column.find_ancestor(exp.Select) is not out:
                continue
            column.set("table", exp.to_identifier(fact_name))
    return out.sql(dialect=dialect), todo


def _unqualified_copy(node: Any) -> Any:
    """Return a copy of ``node`` with every column qualifier stripped."""
    copied = node.copy()
    for column in copied.find_all(exp.Column):
        column.set("table", None)
    return copied


def _is_dimension_projection(projection: Any, dimensions: list[str]) -> bool:
    """Return True when the projection is a standalone column of a rewritten dimension."""
    inner = projection.this if isinstance(projection, exp.Alias) else projection
    return isinstance(inner, exp.Column) and inner.name in dimensions


def _dimension_join_complete(
    projections: list[Any],
    dimensions: list[str],
    mappings: dict[str, dict[str, str]],
    joined_tables: set[str],
    unqualified_names: set[str],
) -> bool:
    """Every dimension must be replaced by its lookup value on a joined table."""
    for name in dimensions:
        mapping = mappings[name]
        has_bare = has_value = False
        for projection in projections:
            inner = projection.this if isinstance(projection, exp.Alias) else projection
            inner = inner.unnest()
            if isinstance(inner, exp.Column) and inner.name == name:
                has_bare = True
            if isinstance(inner, exp.Column) and inner.name == mapping["value_column"]:
                has_value = True
        if has_bare or not has_value or mapping["dimension_table"] not in joined_tables:
            return False
        # A lookup key sharing the fact column's name makes every remaining
        # bare reference ambiguous once the lookup table is joined.
        if mapping["key_column"] == name and name in unqualified_names:
            return False
    return True


def _projections_covered_by_group(projections: list[Any], group: Any, group_names: set[str]) -> bool:
    """Every plain projected column must stay in GROUP BY."""
    for projection in projections:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(inner, exp.Column) and group is not None and inner.name not in group_names:
            return False
    return True


def _where_avoids_value_columns(rewritten: exp.Select, value_columns: set[str]) -> bool:
    """No WHERE predicate may reference a rewritten dimension's value column."""
    where = rewritten.args.get("where")
    return where is None or not any(column.name in value_columns for column in where.find_all(exp.Column))


def _original_projections_preserved(original: exp.Select, projections: list[Any], dimensions: list[str]) -> bool:
    """Non-dimension projections must survive verbatim and no extras may appear."""
    survivors = []
    for projection in original.expressions:
        if not _is_dimension_projection(projection, dimensions):
            survivors.append(_unqualified_copy(projection).sql())
    rewritten_projections = [_unqualified_copy(p).sql() for p in projections]
    if any(sql not in rewritten_projections for sql in survivors):
        return False
    return len(projections) == len(survivors) + len(dimensions)


def _dimension_postconditions(
    original: exp.Select,
    rewritten: exp.Select,
    dimensions: list[str],
    mappings: dict[str, dict[str, str]],
) -> bool:
    """Verify a dimension rewrite completed atomically."""
    projections = rewritten.expressions
    group = rewritten.args.get("group")
    group_names = {e.name for e in group.expressions if isinstance(e, exp.Column)} if group is not None else set()
    joined_tables = {table.name for table in _source_tables(rewritten)}
    unqualified_names = {column.name for column in rewritten.find_all(exp.Column) if not column.table}
    value_columns = {mappings[name]["value_column"] for name in dimensions}
    return (
        _dimension_join_complete(projections, dimensions, mappings, joined_tables, unqualified_names)
        and _projections_covered_by_group(projections, group, group_names)
        and _where_avoids_value_columns(rewritten, value_columns)
        and _original_projections_preserved(original, projections, dimensions)
    )


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
        statement = sqlglot.parse_one(result.sql, read=dialect)
    except sqlglot.errors.SqlglotError as exc:
        logger.warning(f"Dimension detection failed: {exc}")
        result.need_ref = True
        return []
    if not isinstance(statement, exp.Select):
        result.need_ref = False
        return []
    projected = _projected_dimension_columns(statement, mappings)
    lost = _grouped_dimension_columns(statement, mappings, set(projected), _source_tables(statement))
    dimensions = projected + lost
    if not dimensions:
        result.need_ref = False
        return []
    mechanical = _rewrite_dimensions_with_sqlglot(statement, dimensions, mappings, dialect)
    if mechanical is not None:
        rewritten_sql, processed = mechanical
        try:
            rewritten_ast = sqlglot.parse_one(rewritten_sql, read=dialect)
            if isinstance(rewritten_ast, exp.Select) and _dimension_postconditions(
                statement, rewritten_ast, processed, mappings
            ):
                result.sql = rewritten_sql
                result.need_ref = False
                return dimensions
        except sqlglot.errors.SqlglotError as exc:
            logger.warning(f"Deterministic dimension rewrite failed postconditions: {exc}")
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
        rewritten_ast = sqlglot.parse_one(rewritten, read=dialect)
        if not isinstance(rewritten_ast, exp.Select) or not _dimension_postconditions(
            statement, rewritten_ast, dimensions, mappings
        ):
            logger.warning(f"Dimension rewrite failed postconditions; keeping original SQL: {rewritten}")
            result.need_ref = True
            return dimensions
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
