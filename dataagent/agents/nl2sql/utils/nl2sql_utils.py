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
import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

import sqlglot
from sqlglot import exp

from dataagent.agents.nl2sql.errors import LLMOutputParseError
from dataagent.agents.nl2sql.workflow.state import NL2SQLState, Result
from dataagent.utils.constants import DEFAULT_NL2SQL_CELL_TRUNCATE_LENGTH
from dataagent.utils.log import logger

# 匹配未被单引号包裹的 ${...} 模板（如 ${starttime, -5, yyyyMMdd}）
_PLACEHOLDER_BRACE = re.compile(r"(?<!\')\$\{[^}]+\}(?!')")
# 匹配未被单引号包裹的 $var 模板（如 $date）；排除 ${...} 中的 $
_PLACEHOLDER_SIMPLE = re.compile(r"(?<!\')\$(?!\{)[a-zA-Z_][a-zA-Z0-9_]*(?!')")
_DIMENSION_CONFIG_DIR = Path(__file__).resolve().parents[1] / "prompts" / "generator"
_DIMENSION_CONFIG_FILES = {
    "business_twin": "business_twin_dimensions.json",
    "traffic_insight": "traffic_insight_dimensions.json",
}
_Strategy = Literal["prompt", "skeleton", "icl", "dc"]


def normalize_sql(sql: str) -> str:
    """Collapse whitespace and trailing semicolons for SQL equality checks."""
    return re.sub(r"\s+", " ", (sql or "").strip()).strip(" ;").casefold()


def sql_sha256(sql: str) -> str:
    """Return a stable SHA-256 fingerprint for normalized SQL."""
    normalized = re.sub(r"\s+", " ", (sql or "").strip())
    return hashlib.sha256(normalized.encode()).hexdigest()


def iter_semantic_column_payloads(raw: Any) -> list[dict]:
    """Normalize semantic column-search payloads into a list of dicts."""
    if raw is None:
        return []
    outer = raw if isinstance(raw, list) else [raw]
    out: list[dict] = []
    for item in outer:
        if isinstance(item, dict) and item:
            payload = next(iter(item.values()))
            if isinstance(payload, dict):
                out.append(payload)
    return out


def quote_sql_placeholders(sql: str) -> str:
    """Wrap unquoted $ / ${} template placeholders in single quotes."""
    sql = _PLACEHOLDER_BRACE.sub(lambda m: f"'{m.group(0)}'", sql)
    sql = _PLACEHOLDER_SIMPLE.sub(lambda m: f"'{m.group(0)}'", sql)
    return sql


def sql_parser(content: str) -> list[str]:
    """Extract SQL statements from model output text."""
    m = re.findall(r"```sql\s*(.*?)\s*```", content, re.S | re.I)
    if not m:
        raise LLMOutputParseError(f"No SQL block found in model output, raw content: {content}")
    sqls = []
    for sql in m:
        sql = sql.replace("\xa0", " ").strip().rstrip(";")
        sql = re.sub(r"/\*.*?\*/|--.*?$", "", sql, flags=re.S | re.M)
        sql = re.sub(r"\s+", " ", sql).strip()
        sql = quote_sql_placeholders(sql)
        sqls.append(sql)
    return sqls


def json_parser(content: str) -> str:
    """Extract a JSON object/array string from model output."""
    m = re.search(r"```json\s*(.*?)\s*```", content, re.S | re.I)
    if not m:
        raise LLMOutputParseError(f"No JSON block found in model output, raw content: {content}")
    return m.group(1).strip()


def metadata_parser(text: str) -> list[dict[str, set[str]]]:
    """Parse metadata-match model output into table/column sets."""
    blocks = re.findall(r"<res>\s*(.*?)\s*</res>", text, flags=re.S | re.I)
    if not blocks:
        raise LLMOutputParseError(f"No <res> metadata block found in model output, raw content: {text}")
    out: list[dict[str, set[str]]] = []
    try:
        for b in blocks:
            m: dict[str, set[str]] = {}
            for line in b.splitlines():
                line = line.strip()
                if not line or "=>" not in line:
                    continue
                col, rhs = map(str.strip, line.split("=>", 1))
                if "." not in col:
                    raise ValueError(f"Invalid metadata column: {col}")
                t, c = (x.strip('"') for x in col.split(".", 1))
                vals = {v.strip() for v in rhs.split("|") if v.strip()} if rhs else set()
                m[f"{t}.{c}"] = vals
            out.append(m)
    except Exception as exc:
        raise LLMOutputParseError(f"Metadata block parsing failed: {exc}, raw content: {text}") from exc
    return out


def flatten_schema(schema: dict) -> set[str]:
    """Flatten schema IR into a set of table.column identifiers."""
    res = set()
    for t, t_meta in schema.items():
        for c in t_meta["columns"]:
            res.add(f"{t}.{c}")
    return res


def filter_schema(schema: dict, used: set[str]) -> dict:
    """Keep only schema entries referenced by the used identifier set."""
    res = {}
    for t, c in (u.split(".", 1) for u in used):
        if t in schema and c in schema[t]["columns"]:
            res.setdefault(
                t,
                {"description": schema[t].get("description"), "columns": {}},
            )["columns"][c] = schema[t]["columns"][c]
    return res


def snippets_to_str(sql_snippets: list[dict[str, str]]) -> str:
    """Render SQL snippet dicts as a single prompt string."""
    return "\n".join(f"{snip['description']}\n```sql\n{snip['content']}\n```\n" for snip in sql_snippets)


def truncate(v: Any) -> str:
    """Truncate a value for prompt-friendly display."""
    MAX_LEN = DEFAULT_NL2SQL_CELL_TRUNCATE_LENGTH
    s = str(v)
    return f"{s[:MAX_LEN]}..." if len(s) > MAX_LEN else v


def _normalize_type(value_type: str):
    if not value_type:
        return "TEXT"
    vt = value_type.lower()
    if "int" in vt or any(k in vt for k in ["id", "count", "num", "decimal(24,0)"]):
        return "INTEGER"
    if any(k in vt for k in ["float", "double", "price", "amount"]):
        return "REAL"
    if any(k in vt for k in ["date", "time"]):
        return "TEXT"
    return "TEXT"


def format_col(col_name: str):
    """Format a column name for DDL/prompt output."""
    return f"`{col_name}`"


_FORMULA_COLUMN = re.compile(
    r"(?:\b(?:avg|count|max|min|sum)\s*\(\s*|^\s*)([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _formula_column_names(formula: str) -> list[str]:
    """Return concrete column identifiers in formula order."""
    return list(dict.fromkeys(_FORMULA_COLUMN.findall(formula)))


def _assign_relation_formulas(schema_ir, relation_catalog) -> dict[tuple[str, str], list[str]]:
    """Assign each unique derived formula to one referenced schema column."""
    if not relation_catalog:
        return {}

    table_columns = {table_name: set(table_info["columns"]) for table_name, table_info in schema_ir.items()}

    assignments: dict[tuple[str, str], list[str]] = {}
    seen_formulas: set[str] = set()
    for metric_id, metadata in relation_catalog.items():
        if not isinstance(metadata, dict):
            continue
        formula = str(metadata.get("column_value_profile") or "").strip()
        if not formula or formula in seen_formulas:
            continue
        seen_formulas.add(formula)

        identifiers = _formula_column_names(formula)
        required_columns = set(identifiers)
        eligible_tables = [
            table_name
            for table_name, columns in table_columns.items()
            if required_columns and required_columns.issubset(columns)
        ]
        candidates: list[tuple[str, str]] = []
        for identifier in identifiers:
            for table_name in eligible_tables:
                candidates.append((table_name, identifier))
        if not candidates:
            continue

        target = min(candidates, key=lambda location: len(assignments.get(location, [])))
        metric_name = str(metadata.get("column_short_description") or "").strip()
        if not metric_name:
            metric_name = str(metric_id).rsplit(".", 1)[-1]
        assignments.setdefault(target, []).append(f"{metric_name} = {formula}")
    return assignments


def schema_to_ddl(schema_ir, joins=None, relation_catalog=None):
    """Convert schema IR (and joins) into a DDL-like prompt string."""
    relation_formulas = _assign_relation_formulas(schema_ir, relation_catalog)
    fk_map = {}
    if joins:
        for left, right in joins:
            l_tbl, l_col = left.split(".")
            r_tbl, r_col = right.split(".")
            fk_map.setdefault(l_tbl, []).append((l_col, r_tbl, r_col))
    ddl_blocks = []
    for table_name, table_info in schema_ir.items():
        lines = []
        table_stmt = f"CREATE TABLE `{table_name}` (\n"
        for col_name, col_info in table_info["columns"].items():
            col_type = _normalize_type(col_info.get("value_type"))
            col_desc = col_info.get("description", "").strip()
            line = f"    {format_col(col_name)} {col_type}"
            comments = []
            if col_desc:
                comments.append(col_desc)
            vals = col_info.get("example_values")
            if vals:
                comments.append(f"example: {vals}")
            for formula in relation_formulas.get((table_name, col_name), []):
                comments.append(f"relation_formula: {formula}")
            if comments:
                line += f", -- {'; '.join(comments)}"
            else:
                line += ","
            lines.append(line)
        fk_lines = []
        for fk_col, ref_table, ref_col in fk_map.get(table_name, []):
            fk_lines.append(f"    FOREIGN KEY ({format_col(fk_col)}) REFERENCES `{ref_table}`({format_col(ref_col)}),")
        all_lines = lines + fk_lines
        if all_lines:
            all_lines[-1] = all_lines[-1].rstrip(",")
        table_stmt += "\n".join(all_lines)
        table_stmt += "\n);"
        if table_info.get("description"):
            table_stmt = f"-- {table_info['description']}\n" + table_stmt
        ddl_blocks.append(table_stmt)
    return "\n\n".join(ddl_blocks)


@lru_cache(maxsize=2)
def load_dimension_mappings(scenario: str) -> dict[str, dict[str, str]]:
    """Load one scenario's packaged fact-to-dimension mappings."""
    filename = _DIMENSION_CONFIG_FILES.get(str(scenario or ""))
    if filename is None:
        return {}
    path = _DIMENSION_CONFIG_DIR / filename
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        dimensions = payload["dimensions"]
        required = {"dimension_table", "key_column", "value_column"}
        if payload.get("scenario") != scenario or not isinstance(dimensions, dict):
            raise ValueError("scenario or dimensions is invalid")
        if any(not isinstance(item, dict) or not required.issubset(item) for item in dimensions.values()):
            raise ValueError("dimension mapping fields are incomplete")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid NL2SQL dimension config: {path}: {exc}") from exc
    return cast(dict[str, dict[str, str]], dimensions)


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


async def _process_dimension_candidate(
    result: tuple[str, str, str],
    mappings: dict[str, dict[str, str]],
    dialect: str,
    execute_with_llm: Callable[[dict[str, str], str], Awaitable[str]],
) -> tuple[str, str, str, list[str], bool]:
    sql, prompt, strategy = result
    try:
        statement = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError as exc:
        logger.warning(f"Generator dimension detection failed: {exc}")
        return sql, prompt, strategy, [], True
    if not isinstance(statement, exp.Select):
        return sql, prompt, strategy, [], False
    projected = _projected_dimension_columns(statement, mappings)
    lost = _grouped_dimension_columns(statement, mappings, set(projected), _source_tables(statement))
    dimensions = projected + lost
    if not dimensions:
        return sql, prompt, strategy, [], False
    mechanical = _rewrite_dimensions_with_sqlglot(statement, dimensions, mappings, dialect)
    if mechanical is not None:
        rewritten_sql, processed = mechanical
        try:
            rewritten_ast = sqlglot.parse_one(rewritten_sql, read=dialect)
            if isinstance(rewritten_ast, exp.Select) and _dimension_postconditions(
                statement, rewritten_ast, processed, mappings
            ):
                return rewritten_sql, prompt, strategy, dimensions, False
        except sqlglot.errors.SqlglotError as exc:
            logger.warning(f"Deterministic dimension rewrite failed postconditions: {exc}")
    context = {
        "dialect": dialect,
        "sql": sql,
        "mappings": json.dumps(
            {name: mappings[name] for name in dimensions},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    try:
        rewritten = sql_parser(await execute_with_llm(context, "dimension_join_"))[-1]
        rewritten_ast = sqlglot.parse_one(rewritten, read=dialect)
        if not isinstance(rewritten_ast, exp.Select) or not _dimension_postconditions(
            statement, rewritten_ast, dimensions, mappings
        ):
            logger.warning(f"Dimension rewrite failed postconditions; keeping original SQL: {rewritten}")
            return sql, prompt, strategy, dimensions, True
    except Exception as exc:
        logger.warning(f"Generator dimension rewrite failed: {exc}")
        return sql, prompt, strategy, dimensions, True
    return rewritten, prompt, strategy, dimensions, False


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


def _add_dimension_context(
    state: NL2SQLState,
    dimensions: list[str],
    mappings: dict[str, dict[str, str]],
) -> str:
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


async def process_dimension_joins(
    results: list[tuple[str, str, str]],
    state: NL2SQLState,
    *,
    scenario: str,
    dialect: str,
    execute_with_llm: Callable[[dict[str, str], str], Awaitable[str]],
) -> list[Result]:
    """Rewrite mapped projected dimensions and prepare downstream validation context."""
    mappings = load_dimension_mappings(str(scenario or ""))
    if mappings:
        processed = list(
            await asyncio.gather(
                *(_process_dimension_candidate(result, mappings, dialect, execute_with_llm) for result in results)
            )
        )
    else:
        processed = [(*result, [], False) for result in results]
    dimensions = list(dict.fromkeys(name for result in processed for name in result[3]))
    dimension_context = _add_dimension_context(state, dimensions, mappings) if dimensions else ""
    return [
        Result(
            id=index,
            sql=sql,
            prompt=f"{prompt}\n\n{dimension_context}" if dimension_context else prompt,
            strategy=cast(_Strategy, strategy),
            need_ref=need_ref,
        )
        for index, (sql, prompt, strategy, _, need_ref) in enumerate(processed)
    ]
