#!/usr/bin/env python3
"""Expand step2_2 (1:N aggregation + list expansion + dimension join) deterministically.

This module is a **library**, not a CLI: ``step2_2_driver.py`` is the only entry point and
owns the file writing / run manifest, while ``step2_2_verify_gate.py`` shares
:func:`_classify_type` / :func:`_aggregation_for` so the policy is applied identically on
both the generation and the gate side.

Reads, from local files only:

  * ``schema_resolution.json``
    - ``output_database`` / ``user_table`` / ``user_id`` / ``label``
    - ``source_tables``  -> table order + per-table ``user_key``
    - ``key_validation`` -> ``classification`` (``1:1用户表`` / ``1:N行为表`` / ``维度表``)
  * ``step1_output_meta.json``      -> physical column order + ``valueType`` per table
  * ``step2_0_profile_dump.json``   -> per-column ``n_unique`` / ``data_type`` / ``sample_values``
  * ``list_fields.txt``             -> authoritative delimiter-field names (``#`` / ``^``)
  * ``references/aggregation_policy.json`` -> the single source of the type -> aggregation map
  * ``string_cast_plan.txt``        -> optional sampled String CAST decisions
  * term-frequency payload(s)       -> collected rows of ``table_name/column_name/term/freq``

Two passes (both driven by ``step2_2_driver.py``):

  * pass 1 (:func:`build_term_queries`) -> one or more term-frequency SQL files (batched by
    ``term_query_batch_size``), so the Agent never hand-writes the per-field ``FROM`` split
    between the 1:1 wide table and the raw 1:N tables.
  * pass 2 (:func:`expand`)           -> ``step2_2_feature_aggregation_expanded.sql`` (one
    CREATE), ``step2_2_validation_expanded.sql`` (split/combine helpers available for the
    driver), ``step2_2_feature_plan.json``, ``step2_2_deployment_feature_contract.json``
    and ``step2_2_feature_derivation.md``.

Every derivation in both passes is a lookup, never a judgement: which aggregation a column
gets, which columns are dropped, how a term maps to a column suffix, and how the wide table
is spelled all come from the policy file plus the profiling dump.
"""

from __future__ import annotations

import json
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

TEMPLATE_AGGREGATION = SCRIPTS_DIR / "step2_2_feature_aggregation.sql"
TEMPLATE_VALIDATION = SCRIPTS_DIR / "step2_2_validation.sql"
POLICY_JSON = SCRIPTS_DIR.parent / "references" / "aggregation_policy.json"

AGGREGATION_BODY_BLOCK = "/*__AGGREGATION_BODY__*/"
LIST_FIELD_NAMES_BLOCK = "/*__LIST_FIELD_NAMES__*/"
GEO_MATCH_BLOCK = "/*__GEO_COLUMN_MATCH__*/"
ALLOWED_STRING_BLOCK = "/*__ALLOWED_STRING_COLUMNS__*/"

ONE_TO_ONE = "1:1用户表"
ONE_TO_MANY = "1:N行为表"
DIMENSION = "维度表"

CTE_ALIAS_PREFIX = "t"
DIM_ALIAS_PREFIX = "d"
BASE_ALIAS = "w"

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_ANGLE_RE = re.compile(r"<([A-Za-z_][A-Za-z0-9_]*)>")
_IDENT_RE = re.compile(r"[^A-Za-z0-9_]+")
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_]")
TABLE_TYPE_CLASSIFICATION = {
    "user_table": ONE_TO_ONE,
    "one_to_one_table": ONE_TO_ONE,
    "one_to_many_table": ONE_TO_MANY,
    "n_to_one_table": ONE_TO_MANY,
    "event_fact_table": ONE_TO_MANY,
    "pay_event_table": ONE_TO_MANY,
    "dimension_table": DIMENSION,
    "entity_dim_table": DIMENSION,
    "entity_attr_table": DIMENSION,
    "entity_brand_table": DIMENSION,
}

# Placeholders intentionally left for submit-time binding (per references/data_source.md), not
# expand-time: the warehouse's column-catalog table name.
DEFERRED_PLACEHOLDERS = {"catalog_columns", "catalog_tables"}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取 {path}：{exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{path} 必须包含一个 JSON 对象")
    return value


def _load_policy(path: Path = POLICY_JSON) -> dict[str, Any]:
    policy = _read_json(path)
    required = (
        "string_max_unique",
        "id_ratio_threshold",
        "numeric_types",
        "datetime_types",
        "bool_types",
        "low_card_string_types",
        "drop_types",
        "aggregation_rules",
    )
    missing = [key for key in required if key not in policy]
    if missing:
        raise SystemExit(f"{path} 缺少必需键：{', '.join(missing)}")
    return policy


# --------------------------------------------------------------------------------------
# type handling
# --------------------------------------------------------------------------------------


def _strip_nullable(data_type: str) -> tuple[str, bool]:
    text = str(data_type or "").strip()
    if text.startswith("Nullable(") and text.endswith(")"):
        return text[len("Nullable(") : -1].strip(), True
    return text, False


def _base_type(data_type: str) -> str:
    text = data_type.strip()
    if text.startswith("LowCardinality(") and text.endswith(")"):
        return _base_type(text[len("LowCardinality(") : -1])
    return text.split("(", 1)[0].strip()


def _classify_type(data_type: str, policy: dict[str, Any]) -> str:
    """Return one of numeric / datetime / bool / low_card_string / unsupported."""
    inner, _nullable = _strip_nullable(data_type)
    if inner.startswith("LowCardinality("):
        return "low_card_string"
    base = _base_type(inner)
    if base in policy["numeric_types"]:
        return "numeric"
    if base in policy["datetime_types"]:
        return "datetime"
    if base in policy["bool_types"]:
        return "bool"
    if base in policy["low_card_string_types"] or base == "String":
        return "low_card_string"
    for token in policy["drop_types"]:
        if base == token or base.startswith(token):
            return "unsupported"
    return "unsupported"


def _aggregation_for(kind: str, policy: dict[str, Any]) -> dict[str, Any] | None:
    for rule in policy["aggregation_rules"]:
        if str(rule.get("match")) == kind:
            return rule
    return None


def _output_type_for(kind: str, data_type: str, policy: dict[str, Any]) -> str:
    inner, _nullable = _strip_nullable(data_type)
    if kind == "numeric":
        base = _base_type(inner)
        return base if base in policy["numeric_types"] else "Float64"
    if kind == "datetime":
        return _base_type(inner)
    if kind == "bool":
        return "UInt8"
    return "LowCardinality(String)"


# --------------------------------------------------------------------------------------
# term -> column suffix
# --------------------------------------------------------------------------------------


def _lazy_pinyin() -> Any:
    try:
        from pypinyin import lazy_pinyin  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency guard
        raise SystemExit(
            "缺少 pypinyin：中文词项的列名后缀需要它生成拼音。"
            "请安装 pypinyin（已在 runtime/dataagent/pyproject.toml 声明）。"
            f"原始错误：{exc}"
        ) from exc
    return lazy_pinyin


def _term_slug(term: str) -> str:
    """Deterministic ASCII column suffix for a term (Chinese terms become pinyin)."""
    text = str(term or "").strip()
    if not text:
        return ""
    if _ASCII_TOKEN_RE.search(text) and not any(ord(ch) > 127 for ch in text):
        slug = text
    else:
        slug = "".join(_lazy_pinyin()(text))
    slug = _IDENT_RE.sub("_", slug).strip("_").lower()
    if not slug:
        return ""
    if slug[0].isdigit():
        slug = f"t{slug}"
    return slug


def _unique_slug(slug: str, used: set[str]) -> str:
    base = slug or "term"
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    used.add(candidate)
    return candidate


# --------------------------------------------------------------------------------------
# schema / meta / dump accessors
# --------------------------------------------------------------------------------------


def _source_tables(schema: dict[str, Any]) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for item in schema.get("source_tables") or []:
        if isinstance(item, dict) and item.get("table_name"):
            tables.append(item)
        elif isinstance(item, str) and item.strip():
            tables.append({"table_name": item.strip()})
    return tables


def _key_validation(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = schema.get("key_validation")
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def _classification(schema: dict[str, Any], table_name: str, table: dict[str, Any]) -> str:
    validation = _key_validation(schema).get(table_name) or {}
    value = str(validation.get("classification") or "").strip()
    if value in {ONE_TO_ONE, ONE_TO_MANY, DIMENSION}:
        return value
    table_type = str(table.get("table_type") or "").strip()
    return TABLE_TYPE_CLASSIFICATION.get(table_type, "")


def _columns_by_table(meta: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for table in meta.get("tables") or []:
        if not isinstance(table, dict):
            continue
        name = str(table.get("name") or "").strip()
        if not name:
            continue
        columns: list[dict[str, str]] = []
        for column in table.get("columns") or []:
            if not isinstance(column, dict) or not column.get("name"):
                continue
            columns.append(
                {
                    "name": str(column["name"]).strip(),
                    "type": str(column.get("valueType") or column.get("type") or "Unknown").strip()
                    or "Unknown",
                }
            )
        result[name] = columns
    return result


def _profile_by_table(dump: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for row in dump.get("column_profile") or []:
        if not isinstance(row, dict):
            continue
        table = str(row.get("table_name") or "").strip()
        column = str(row.get("column_name") or "").strip()
        if table and column:
            result.setdefault(table, {})[column] = row
    return result


def _table_rows(dump: dict[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in dump.get("table_profile") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("table_name") or "").strip()
        total = row.get("total_rows")
        if name and isinstance(total, int):
            result[name] = total
    return result


def _user_key_columns(meta: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    aliases = schema.get("user_id_columns")
    if not isinstance(aliases, list):
        aliases = ((meta.get("column_aliases") or {}).get("user_id_columns")) or []
    ordered: list[str] = []
    seen: set[str] = set()
    for item in aliases:
        name = str(item).strip()
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _resolve_user_key(table: dict[str, Any], columns: list[str], aliases: list[str]) -> str | None:
    explicit = str(table.get("user_key") or "").strip()
    if explicit:
        return explicit
    column_set = set(columns)
    for alias in aliases:
        if alias in column_set:
            return alias
    return None


def _delimiter_of(sample_values: Any, accepted: tuple[str, ...] = ("#", "^")) -> str:
    text = str(sample_values or "")
    for token in accepted:
        if token in text:
            return token
    return ""


def _read_list_fields(path: Path) -> list[str]:
    if not path.is_file():
        return []
    names: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        name = line.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _read_string_plan(path: Path | None) -> dict[str, str]:
    """Parse ``string_cast_plan.txt``: ``<column> <rule> [ratio]`` per line."""
    if path is None or not path.is_file():
        return {}
    plan: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        parts = text.split()
        if len(parts) < 2:
            continue
        plan[parts[0]] = parts[1].lower()
    return plan


# --------------------------------------------------------------------------------------
# payload rows
# --------------------------------------------------------------------------------------


def _load_payloads(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not text:
        return []
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        pass
    payloads: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not (line.startswith("{") or line.startswith("[")):
            continue
        try:
            payloads.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if payloads:
        return payloads
    start = min((index for index in (text.find("["), text.find("{")) if index >= 0), default=-1)
    if start >= 0:
        try:
            return [json.loads(text[start:])]
        except json.JSONDecodeError:
            return []
    return []


def _extract_results(payload: Any) -> list[tuple[list[str], list[Any]]]:
    found: list[tuple[list[str], list[Any]]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            columns = node.get("columns")
            rows = node.get("rows")
            if isinstance(columns, list) and isinstance(rows, list):
                found.append(([str(col) for col in columns], rows))
                return
            for key in ("outputs", "data", "result", "payload", "original_msg"):
                if key in node:
                    _walk(node[key])
            return
        if isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)
    return found


def _rows(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            raise SystemExit(f"未找到 payload 文件：{path}")
        for payload in _load_payloads(path):
            results = _extract_results(payload)
            if results:
                for columns, rows in results:
                    for row in rows:
                        if isinstance(row, dict):
                            records.append(dict(row))
                        elif isinstance(row, (list, tuple)):
                            records.append(
                                {
                                    str(columns[index]): (row[index] if index < len(row) else None)
                                    for index in range(len(columns))
                                }
                            )
                continue
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                records.extend(dict(item) for item in payload)
    return records


def _terms_by_field(term_rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[tuple[str, int]]]:
    """Group term payload rows into ``(table, column) -> [(term, freq), ...]`` desc by freq."""
    grouped: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for row in term_rows:
        table = str(row.get("table_name") or "").strip()
        column = str(row.get("column_name") or "").strip()
        term = row.get("term")
        if not table or not column or term is None:
            continue
        try:
            freq = int(float(row.get("freq") or 0))
        except (TypeError, ValueError):
            freq = 0
        grouped.setdefault((table, column), []).append((str(term), freq))
    for key, values in grouped.items():
        values.sort(key=lambda item: (-item[1], item[0]))
    return grouped


def _take_terms(terms: list[tuple[str, int]], policy: dict[str, Any]) -> list[str]:
    """Apply the policy's total->take rule (<=10 all, <=100 top-10, else top-50)."""
    total = len(terms)
    take: Any = 10
    for rule in policy.get("term_top_by_total") or []:
        ceiling = rule.get("max_terms")
        if ceiling is None or total <= int(ceiling):
            take = rule.get("take")
            break
    if take == "all" or take is None:
        return [term for term, _freq in terms]
    return [term for term, _freq in terms[: int(take)]]


# --------------------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------------------


@dataclass
class ColumnPlan:
    name: str
    data_type: str
    kind: str
    keep: bool
    aggregation: str | None = None
    null_policy: dict[str, Any] | None = None
    reason: str | None = None


@dataclass
class ListFieldPlan:
    table: str
    column: str
    delimiter: str
    terms: list[str] = field(default_factory=list)
    suffixes: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class TablePlan:
    table: str
    classification: str
    user_key: str
    alias: str
    cte_name: str
    columns: list[ColumnPlan] = field(default_factory=list)
    dropped: list[dict[str, str]] = field(default_factory=list)
    list_fields: list[ListFieldPlan] = field(default_factory=list)


@dataclass
class ExpandResult:
    aggregation_sql: str
    validation_sql: str
    plan: dict[str, Any]
    contract: dict[str, Any]
    derivation_md: str


def _plan_table_columns(
    table: str,
    columns: list[dict[str, str]],
    profile: dict[str, dict[str, Any]],
    total_rows: int | None,
    user_key: str,
    policy: dict[str, Any],
    list_field_names: set[str] | None = None,
) -> tuple[list[ColumnPlan], list[dict[str, str]]]:
    """Plan the non-list columns of a 1:N table.

    ``list_field_names`` are handled by the delimiter branch (term expansion + length /
    emptiness), so they must not enter the type -> aggregation table at all: neither kept
    nor listed as ``dropped`` (the raw column is replaced, not discarded). The user key is
    still reported as ``already_aggregated_column`` so the GROUP BY key stays auditable.
    """
    list_field_names = list_field_names or set()
    kept: list[ColumnPlan] = []
    dropped: list[dict[str, str]] = []
    for column in columns:
        name = column["name"]
        data_type = column["type"]
        record = profile.get(name) or {}
        dump_type = str(record.get("data_type") or "").strip()
        effective_type = dump_type or data_type
        if name == user_key:
            dropped.append({"column": name, "reason": "already_aggregated_column"})
            continue
        if name in list_field_names:
            continue
        kind = _classify_type(effective_type, policy)
        if kind == "unsupported":
            dropped.append({"column": name, "reason": "unsupported_type"})
            continue
        n_unique = record.get("n_unique")
        if (
            isinstance(n_unique, int)
            and isinstance(total_rows, int)
            and total_rows > 0
            and n_unique / total_rows >= float(policy["id_ratio_threshold"])
        ):
            dropped.append({"column": name, "reason": "identifier_like"})
            continue
        if kind == "low_card_string":
            if not isinstance(n_unique, int) or n_unique > int(policy["string_max_unique"]):
                dropped.append({"column": name, "reason": "high_cardinality_string"})
                continue
        rule = _aggregation_for(kind, policy)
        if rule is None:
            dropped.append({"column": name, "reason": "unsupported_type"})
            continue
        kept.append(
            ColumnPlan(
                name=name,
                data_type=effective_type,
                kind=kind,
                keep=True,
                aggregation=str(rule["aggregation"]),
                null_policy=dict(rule.get("null_policy") or {"kind": "preserve"}),
            )
        )
    return kept, dropped


def _plan_list_field(
    table: str,
    column: str,
    delimiter: str,
    terms: list[tuple[str, int]],
    policy: dict[str, Any],
) -> ListFieldPlan:
    selected = _take_terms(terms, policy)
    suffixes: list[tuple[str, str]] = []
    used: set[str] = set()
    for term in selected:
        slug = _unique_slug(_term_slug(term), used)
        suffixes.append((term, slug))
    return ListFieldPlan(
        table=table,
        column=column,
        delimiter=delimiter,
        terms=selected,
        suffixes=suffixes,
    )


# --------------------------------------------------------------------------------------
# 1:N CTE feature records
# --------------------------------------------------------------------------------------


def _quote(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _aggregate_expression(column: ColumnPlan, alias: str) -> str:
    return f"{column.aggregation}({alias}.{column.name})"


def _term_expression(alias: str, column: str, delimiter: str, term: str, aggregate: bool) -> str:
    inner = (
        f"has(splitByChar({_quote(delimiter)}, assumeNotNull({alias}.{column})), {_quote(term)})"
    )
    return f"max({inner})" if aggregate else inner


def _list_length_expression(alias: str, column: str, delimiter: str, aggregate: bool) -> str:
    inner = f"length(splitByChar({_quote(delimiter)}, assumeNotNull({alias}.{column})))"
    return f"max({inner})" if aggregate else inner


def _list_empty_expression(alias: str, column: str, aggregate: bool) -> str:
    inner = f"toUInt8({alias}.{column} IS NULL OR toString({alias}.{column}) = '')"
    return f"max({inner})" if aggregate else inner


def _render_cte(table_plan: TablePlan, output_database: str, user_id: str) -> str:
    alias = table_plan.alias
    lines: list[str] = ["SELECT", f"    {alias}.{table_plan.user_key} AS {user_id}"]
    for column in table_plan.columns:
        lines.append(
            f"    , {_aggregate_expression(column, alias)} AS {table_plan.table}_{column.name}"
        )
    for list_field in table_plan.list_fields:
        column = list_field.column
        delimiter = list_field.delimiter
        for term, slug in list_field.suffixes:
            lines.append(
                f"    , {_term_expression(alias, column, delimiter, term, True)} "
                f"AS {table_plan.table}_{column}_term_{slug}"
            )
        lines.append(
            f"    , {_list_length_expression(alias, column, delimiter, True)} "
            f"AS {table_plan.table}_{column}_list_length"
        )
        lines.append(
            f"    , {_list_empty_expression(alias, column, True)} "
            f"AS {table_plan.table}_{column}_is_empty"
        )
    lines.append(f"    , count() AS {table_plan.table}_row_count")
    lines.append(f"FROM {output_database}.{table_plan.table} AS {alias}")
    lines.append(f"GROUP BY {table_plan.user_key}")
    return "\n".join(lines)


def _render_body(
    ctes: list[TablePlan],
    dims: list[dict[str, Any]],
    output_database: str,
    user_id: str,
    except_columns: list[str],
    outer_columns: list[str],
    outer_except: list[str] | None = None,
) -> str:
    """``WITH`` the per-table aggregation CTEs, then a 1-JOIN-per-layer nested chain."""
    except_clause = ""
    if except_columns:
        except_clause = " EXCEPT (" + ", ".join(except_columns) + ")"
    # Columns produced by the CTEs only exist after the joins, so their EXCEPT belongs on
    # the outermost projection, not on the base-table star.
    outer_except_clause = ""
    if outer_except:
        outer_except_clause = " EXCEPT (" + ", ".join(dict.fromkeys(outer_except)) + ")"

    with_clause = ""
    if ctes:
        cte_texts = [
            "    " + table_plan.cte_name + " AS (\n"
            + textwrap.indent(_render_cte(table_plan, output_database, user_id), "        ")
            + "\n    )"
            for table_plan in ctes
        ]
        with_clause = "WITH\n" + ",\n".join(cte_texts) + "\n"

    sql: str | None = None
    last_alias = BASE_ALIAS
    layer = 0
    for table_plan in ctes:
        layer += 1
        prev = last_alias
        if sql is None:
            from_expr = f"{output_database}.step2_1_wide_joined AS {BASE_ALIAS}"
            left_star = f"{BASE_ALIAS}.*{except_clause}"
        else:
            from_expr = "(\n" + textwrap.indent(sql, "    ") + f"\n) AS {prev}"
            left_star = f"{prev}.*"
        sql = (
            f"SELECT\n    {left_star},\n    {table_plan.alias}.* EXCEPT ({user_id})\n"
            f"FROM {from_expr}\n"
            f"LEFT JOIN {table_plan.cte_name} AS {table_plan.alias} "
            f"ON {prev}.{user_id} = {table_plan.alias}.{user_id}"
        )
        last_alias = f"j{layer}"

    for dim in dims:
        layer += 1
        prev = last_alias
        if sql is None:
            from_expr = f"{output_database}.step2_1_wide_joined AS {BASE_ALIAS}"
            left_star = f"{BASE_ALIAS}.*{except_clause}"
        else:
            from_expr = "(\n" + textwrap.indent(sql, "    ") + f"\n) AS {prev}"
            left_star = f"{prev}.*"
        dim_except = list(dim.get("geo_columns") or [])
        if user_id in (dim.get("columns") or []) and user_id not in dim_except:
            dim_except.append(user_id)
        project = [
            f"{dim['alias']}.*"
            + (f" EXCEPT ({', '.join(dim_except)})" if dim_except else "")
        ]
        project.extend(dim["tier_expressions"])
        row_limit = dim.get("row_limit")
        if row_limit:
            right = (
                f"(SELECT * FROM {output_database}.{dim['table']} LIMIT {int(row_limit)}) "
                f"AS {dim['alias']}"
            )
        else:
            right = f"{output_database}.{dim['table']} AS {dim['alias']}"
        sql = (
            f"SELECT\n    {left_star},\n    " + ",\n    ".join(project) + "\n"
            f"FROM {from_expr}\n"
            f"CROSS JOIN {right}"
        )
        last_alias = f"j{layer}"

    if sql is None:
        # No join layers: every EXCEPT column (including those moved into outer_except
        # by the alias-bug workaround) must land on the base star.
        sole = list(dict.fromkeys([*(except_columns or []), *(outer_except or [])]))
        sole_clause = (" EXCEPT (" + ", ".join(sole) + ")") if sole else ""
        head = f"SELECT\n    {BASE_ALIAS}.*{sole_clause}"
        tail = f"\nFROM {output_database}.step2_1_wide_joined AS {BASE_ALIAS}"
        outer_ref = BASE_ALIAS
    else:
        head = f"SELECT\n    {last_alias}.*{outer_except_clause}"
        tail = f"\nFROM (\n{textwrap.indent(sql, '    ')}\n) AS {last_alias}"
        outer_ref = last_alias

    extras = [line.replace("{alias}", outer_ref) for line in outer_columns]
    if extras:
        head = head + "\n    , " + "\n    , ".join(extras)
    return with_clause + head + tail


def _render_string_cast(
    column: str,
    rule: str,
    policy: dict[str, Any],
) -> list[str]:
    """Outer SELECT expressions for one sampled String column.

    ``{alias}`` is left for ``_render_body`` to bind to the outermost subquery alias.
    """
    rules = policy.get("string_cast_rules") or {}
    if rule in {"numeric", "datetime", "lowcard"}:
        key = "low_cardinality" if rule == "lowcard" else rule
        spec = rules.get(key) or {}
        expression = str(spec.get("expression") or "{col}").replace("{col}", f"{{alias}}.{column}")
        return [f"{expression} AS {column}"]
    if rule == "comma_list":
        return [
            f"length(splitByChar(',', assumeNotNull({{alias}}.{column}))) AS {column}_list_length",
            f"toUInt8({{alias}}.{column} IS NULL OR toString({{alias}}.{column}) = '') "
            f"AS {column}_is_empty",
        ]
    return []


def _replace_outside_comments(template: str, replacements: dict[str, str]) -> str:
    lines: list[str] = []
    for line in template.splitlines():
        if line.lstrip().startswith("--"):
            lines.append(line)
            continue

        def _sub(match: re.Match[str]) -> str:
            return replacements.get(match.group(1), match.group(0))

        lines.append(_ANGLE_RE.sub(_sub, _PLACEHOLDER_RE.sub(_sub, line)))
    return "\n".join(lines)


def _replace_blocks_outside_comments(template: str, replacements: dict[str, str]) -> str:
    """Substitute ``/*__BLOCK__*/`` markers on non-comment lines only.

    ``_replace_outside_comments`` guards ``{{...}}`` / ``<...>`` but a bare ``str.replace``
    would also hit the marker when a template documents it inside a ``--`` comment. When the
    payload spans lines (e.g. the geo matcher) that injects bare SQL into the comment block,
    corrupting the statement. Bind markers the same way: never inside a comment.
    """
    lines: list[str] = []
    for line in template.splitlines():
        if line.lstrip().startswith("--"):
            lines.append(line)
            continue
        for marker, value in replacements.items():
            line = line.replace(marker, value)
        lines.append(line)
    return "\n".join(lines)


def _non_comment_text(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))


def split_validation_sql(sql: str) -> list[str]:
    """Split a multi-statement validation script on ``FORMAT JSON;`` boundaries."""
    marker = "FORMAT JSON;"
    parts: list[str] = []
    remaining = sql
    while True:
        idx = remaining.find(marker)
        if idx < 0:
            leftover = remaining.strip()
            if leftover:
                parts.append(leftover if leftover.endswith(";") else leftover + ";")
            break
        chunk = remaining[: idx + len(marker)].strip()
        if chunk:
            parts.append(chunk)
        remaining = remaining[idx + len(marker) :]
    if not parts:
        raise SystemExit("validation SQL 未生成任何可提交的语句")
    return parts


def combine_validation_sql(parts: list[str]) -> str:
    """Merge single-row SELECT gates with CROSS JOIN into one statement.

    Always emits ``SELECT 'step2_2_validation' AS gate, g1.*, ... FROM (...) AS g1
    CROSS JOIN ... FORMAT JSON;`` so ``raw_wide_stats.json`` carries a ``gate`` field.
    Each part must be a single-row SELECT (no ``GROUP BY``).
    """
    cleaned: list[str] = []
    for index, part in enumerate(parts):
        text = str(part or "").strip()
        if not text:
            continue
        text = re.sub(r"\s*FORMAT\s+JSON\s*;\s*$", "", text, flags=re.IGNORECASE).rstrip()
        text = text.rstrip(";").rstrip()
        active = _non_comment_text(text)
        if re.search(r"\bGROUP\s+BY\b", active, re.IGNORECASE):
            raise SystemExit(
                f"combine_validation_sql: 第 {index + 1} 段门禁含 GROUP BY，无法 CROSS JOIN 合并"
            )
        cleaned.append(text)
    if not cleaned:
        raise SystemExit("combine_validation_sql: 没有可合并的 validation SELECT")
    aliases = [f"g{i}" for i in range(1, len(cleaned) + 1)]
    select_list = ", ".join(
        ["'step2_2_validation' AS gate"] + [f"{alias}.*" for alias in aliases]
    )
    from_clause = f"(\n{cleaned[0]}\n) AS {aliases[0]}"
    for alias, body in zip(aliases[1:], cleaned[1:]):
        from_clause += f"\nCROSS JOIN (\n{body}\n) AS {alias}"
    return f"SELECT\n    {select_list}\nFROM {from_clause}\nFORMAT JSON;"


def _assert_expanded(path: Path, text: str) -> None:
    active = _non_comment_text(text)
    if "/*__" in active:
        raise SystemExit(f"{path} 仍含未展开的 /*__...__*/ 动态块")
    leftover = _PLACEHOLDER_RE.search(active)
    if leftover:
        raise SystemExit(f"{path} 仍含未替换占位符 {leftover.group(0)}")
    for match in _ANGLE_RE.finditer(active):
        if match.group(1) not in DEFERRED_PLACEHOLDERS:
            raise SystemExit(f"{path} 仍含未替换占位符 {match.group(0)}")


# --------------------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------------------


def _classification_buckets(
    schema: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    one_to_one: list[dict[str, Any]] = []
    one_to_many: list[dict[str, Any]] = []
    dims: list[dict[str, Any]] = []
    for table in _source_tables(schema):
        name = str(table.get("table_name") or "").strip()
        if not name:
            continue
        bucket = _classification(schema, name, table)
        if bucket == ONE_TO_ONE:
            one_to_one.append(table)
        elif bucket == ONE_TO_MANY:
            one_to_many.append(table)
        elif bucket == DIMENSION:
            dims.append(table)
    return one_to_one, one_to_many, dims


def _wide_columns(
    schema: dict[str, Any],
    columns_by_table: dict[str, list[dict[str, str]]],
    meta: dict[str, Any],
) -> list[str]:
    """Reproduce step2_1's wide-table spelling: base verbatim, right tables prefixed."""
    base_table = str(schema.get("user_table") or "").strip()
    aliases = _user_key_columns(meta, schema)
    wide: list[str] = [column["name"] for column in columns_by_table.get(base_table, [])]
    for table in _source_tables(schema):
        name = str(table.get("table_name") or "").strip()
        if not name or name == base_table:
            continue
        if _classification(schema, name, table) != ONE_TO_ONE:
            continue
        columns = [column["name"] for column in columns_by_table.get(name, [])]
        key = _resolve_user_key(table, columns, aliases)
        for column in columns:
            if column == key:
                continue
            wide.append(f"{name}_{column}")
    return wide


def expand(
    schema: dict[str, Any],
    meta: dict[str, Any],
    dump: dict[str, Any],
    list_fields: list[str],
    term_records: list[dict[str, Any]],
    string_plan: dict[str, str],
    policy: dict[str, Any],
) -> ExpandResult:
    output_database = str(schema.get("output_database") or "").strip()
    base_table = str(schema.get("user_table") or "").strip()
    user_id = str(schema.get("user_id") or "").strip()
    label = str(schema.get("label") or "").strip()
    if not output_database:
        raise SystemExit("schema_resolution.output_database 为空，无法填充 {{output_database}}")
    if not base_table:
        raise SystemExit("schema_resolution.user_table 为空，无法确定基表")
    if not user_id:
        raise SystemExit("schema_resolution.user_id 为空，无法填充 ORDER BY <user_id>")

    columns_by_table = _columns_by_table(meta)
    if not columns_by_table.get(base_table):
        raise SystemExit(f"step1_output_meta.json 中找不到基表 {base_table} 的列清单")
    profile_by_table = _profile_by_table(dump)
    totals = _table_rows(dump)
    aliases = _user_key_columns(meta, schema)

    one_to_one, one_to_many, dim_tables = _classification_buckets(schema)
    wide = _wide_columns(schema, columns_by_table, meta)
    wide_set = set(wide)

    # Resolve every delimiter field to its owning table via the profiling dump, then map to
    # the wide-table spelling the same way step2_1 does (base verbatim, others prefixed).
    list_field_table: dict[str, str] = {}
    for table_name, columns in profile_by_table.items():
        for column_name in columns:
            if column_name in list_fields and column_name not in list_field_table:
                list_field_table[column_name] = table_name
    missing_fields = [name for name in list_fields if name not in list_field_table]
    if missing_fields:
        raise SystemExit(
            "list_fields.txt 中的字段在 step2_0_profile_dump.json 里找不到所属表："
            + ", ".join(missing_fields)
        )

    def wide_spelling(owner: str, column: str) -> str:
        return column if owner == base_table else f"{owner}_{column}"

    terms = _terms_by_field(term_records)

    # ---- 1:N tables: aggregate + list-expand inside each CTE ----------------------------
    table_plans: list[TablePlan] = []
    for index, table in enumerate(one_to_many, start=1):
        name = str(table.get("table_name") or "").strip()
        columns = columns_by_table.get(name) or []
        if not columns:
            raise SystemExit(f"{name}：step1_output_meta.json 中找不到该表的列清单")
        plain = [column["name"] for column in columns]
        key = _resolve_user_key(table, plain, aliases)
        if not key or key not in plain:
            raise SystemExit(f"{name}：无法解析用户键，1:N 聚合无法生成 GROUP BY")
        profile = profile_by_table.get(name) or {}
        kept, dropped = _plan_table_columns(
            name,
            columns,
            profile,
            totals.get(name),
            key,
            policy,
            {field for field in list_fields if list_field_table.get(field) == name},
        )
        plan = TablePlan(
            table=name,
            classification=ONE_TO_MANY,
            user_key=key,
            alias=f"{CTE_ALIAS_PREFIX}{index}",
            cte_name=f"{name}_agg",
            columns=kept,
            dropped=dropped,
        )
        for field_name in list_fields:
            if list_field_table.get(field_name) != name:
                continue
            record = profile.get(field_name) or {}
            delimiter = _delimiter_of(record.get("sample_values"))
            if not delimiter:
                continue
            plan.list_fields.append(
                _plan_list_field(
                    name, field_name, delimiter, terms.get((name, field_name), []), policy
                )
            )
        table_plans.append(plan)

    # ---- dimensions: one CROSS JOIN layer each, geo columns scrubbed per layer ---------
    dim_plans: list[dict[str, Any]] = []
    seen_dims: set[str] = set()
    # Missing key / 0 => do not force LIMIT 1 (contract default); policy file usually sets 1.
    dim_row_threshold = int(policy.get("dimension_row_limit_threshold") or 0)
    for table in dim_tables:
        name = str(table.get("table_name") or "").strip()
        if not name or name in seen_dims:
            continue
        seen_dims.add(name)
        dim_rows = totals.get(name)
        # A zero-row dimension CROSS JOIN zeroes the whole wide table; skip it.
        if isinstance(dim_rows, int) and dim_rows <= 0:
            continue
        alias = f"{DIM_ALIAS_PREFIX}{len(dim_plans) + 1}"
        dim_columns = [column["name"] for column in columns_by_table.get(name, [])]
        dim_geo = _geo_columns(dim_columns)
        tier_expressions: list[str] = []
        tier_outputs: list[dict[str, str]] = []
        for column in dim_geo:
            output = f"{name}_{column}_tier"
            expression = _city_tier_expression(f"{alias}.{column}", output)
            if expression is None:
                continue
            tier_expressions.append(expression)
            tier_outputs.append({"column": column, "output": output, "expression": expression})
        row_limit: int | None = None
        if isinstance(dim_rows, int) and dim_rows > dim_row_threshold:
            row_limit = 1
        dim_plans.append(
            {
                "table": name,
                "alias": alias,
                "columns": dim_columns,
                "geo_columns": dim_geo,
                "tier_expressions": tier_expressions,
                "tier_outputs": tier_outputs,
                "rows": dim_rows,
                "row_limit": row_limit,
            }
        )

    # ---- outer select: 1:1 list expansion + sampled String CAST ----------------------
    # Every feature record is written at the same moment its SQL is rendered, so the
    # deployment contract below is a serialization of these records, not a re-derivation.
    one_to_one_names = {str(table.get("table_name") or "").strip() for table in one_to_one}
    except_columns: list[str] = []
    outer_columns: list[str] = []
    features: dict[str, dict[str, Any]] = {}
    base_plan_id = f"{base_table}_base"

    def one_to_one_owner(wide_column: str) -> tuple[str, str]:
        if wide_column in {column["name"] for column in columns_by_table.get(base_table, [])}:
            return base_table, wide_column
        for candidate in one_to_one:
            name = str(candidate.get("table_name") or "").strip()
            if not name or name == base_table:
                continue
            prefix = f"{name}_"
            if wide_column.startswith(prefix):
                bare = wide_column[len(prefix) :]
                if bare in {column["name"] for column in columns_by_table.get(name, [])}:
                    return name, bare
        return base_table, wide_column

    def physical_ref(wide_column: str) -> tuple[str, str]:
        """Map a wide-table column back to its physical owner table + bare column.

        step2_1_wide_joined stores 1:1 columns prefixed, so the wide spelling and the
        physical ``owner.column`` differ. The contract must be expressed against physical
        tables under aliases the entity plan actually declares (``w`` is a step2_2-local
        alias and is forbidden in the contract), while the SQL reads the already-merged
        wide table under alias ``w``.
        """
        owner, bare = one_to_one_owner(wide_column)
        return (base_table if owner == base_table else owner), bare

    def add_feature(
        name: str,
        plan_id: str,
        expression: str,
        source_columns: list[dict[str, str]],
        output_type: str,
        null_policy: dict[str, Any],
    ) -> None:
        features[name] = {
            "relation_plan": plan_id,
            "expression": expression,
            "source_columns": source_columns,
            "output_type": output_type,
            "null_policy": null_policy,
        }

    listed_1to1: list[dict[str, Any]] = []
    for field_name in list_fields:
        owner = list_field_table.get(field_name)
        if owner not in one_to_one_names:
            continue
        record = (profile_by_table.get(owner) or {}).get(field_name) or {}
        delimiter = _delimiter_of(record.get("sample_values"))
        if not delimiter:
            continue
        spelling = wide_spelling(owner, field_name)
        if spelling not in wide_set:
            continue
        except_columns.append(spelling)
        field_plan = _plan_list_field(
            owner, field_name, delimiter, terms.get((owner, field_name), []), policy
        )
        listed_1to1.append(
            {
                "table": owner,
                "column": field_name,
                "wide_column": spelling,
                "delimiter": delimiter,
                "terms": [{"term": term, "suffix": slug} for term, slug in field_plan.suffixes],
            }
        )
        alias, bare = physical_ref(spelling)
        for term, slug in field_plan.suffixes:
            expression = _term_expression("{alias}", spelling, delimiter, term, False)
            outer_columns.append(f"{expression} AS {spelling}_term_{slug}")
            add_feature(
                f"{spelling}_term_{slug}",
                base_plan_id,
                _term_expression(alias, bare, delimiter, term, False),
                [{"alias": alias, "column": bare}],
                "UInt8",
                {"kind": "preserve"},
            )
        length_expression = _list_length_expression("{alias}", spelling, delimiter, False)
        outer_columns.append(f"{length_expression} AS {spelling}_list_length")
        add_feature(
            f"{spelling}_list_length",
            base_plan_id,
            _list_length_expression(alias, bare, delimiter, False),
            [{"alias": alias, "column": bare}],
            "UInt64",
            {"kind": "preserve"},
        )
        empty_expression = f"toUInt8({{alias}}.{spelling} IS NULL OR toString({{alias}}.{spelling}) = '')"
        outer_columns.append(f"{empty_expression} AS {spelling}_is_empty")
        add_feature(
            f"{spelling}_is_empty",
            base_plan_id,
            f"toUInt8({alias}.{bare} IS NULL OR toString({alias}.{bare}) = '')",
            [{"alias": alias, "column": bare}],
            "UInt8",
            {"kind": "preserve"},
        )

    cast_columns: list[dict[str, str]] = []
    geo_owned = set(_geo_columns(wide))
    for column in wide:
        rule = string_plan.get(column)
        if not rule:
            continue
        if column in {user_id, label}:
            continue
        if column in except_columns or column in geo_owned:
            # geo columns are rendered by the ``{col}_tier`` branch below; a cast rule must
            # not pull them into ``except_columns`` without producing a replacement.
            continue
        record = _profile_for_wide(column, base_table, one_to_one, columns_by_table, profile_by_table)
        kind = _classify_type(str((record or {}).get("data_type") or ""), policy)
        alias, bare = physical_ref(column)
        if rule == "comma_list":
            if kind != "low_card_string":
                continue
            except_columns.append(column)
            outer_columns.append(
                f"length(splitByChar(',', assumeNotNull({{alias}}.{column}))) "
                f"AS {column}_list_length"
            )
            add_feature(
                f"{column}_list_length",
                base_plan_id,
                f"length(splitByChar(',', assumeNotNull({alias}.{bare})))",
                [{"alias": alias, "column": bare}],
                "UInt64",
                {"kind": "preserve"},
            )
            outer_columns.append(
                f"toUInt8({{alias}}.{column} IS NULL OR toString({{alias}}.{column}) = '') "
                f"AS {column}_is_empty"
            )
            add_feature(
                f"{column}_is_empty",
                base_plan_id,
                f"toUInt8({alias}.{bare} IS NULL OR toString({alias}.{bare}) = '')",
                [{"alias": alias, "column": bare}],
                "UInt8",
                {"kind": "preserve"},
            )
            cast_columns.append({"column": column, "rule": rule})
            continue
        if kind != "low_card_string":
            continue
        except_columns.append(column)
        expressions = _render_string_cast(column, rule, policy)
        if not expressions:
            continue
        outer_columns.extend(expressions)
        cast_columns.append({"column": column, "rule": rule})
        spec = (policy.get("string_cast_rules") or {}).get(
            "low_cardinality" if rule == "lowcard" else rule
        ) or {}
        contract_expression = (
            str(spec.get("expression") or "{col}")
            .replace("{col}", f"{alias}.{bare}")
            .rsplit(" AS ", 1)[0]
        )
        add_feature(
            column,
            base_plan_id,
            contract_expression,
            [{"alias": alias, "column": bare}],
            str(spec.get("output_type") or "LowCardinality(String)"),
            dict(spec.get("null_policy") or {"kind": "preserve"}),
        )

    # ---- geo tier ---------------------------------------------------------------------
    geo_columns = _geo_columns(wide)
    tier_columns: list[dict[str, str]] = []
    for column in geo_columns:
        if column in except_columns:
            continue
        output_alias = f"{column}_tier"
        alias, bare = physical_ref(column)
        expression = _city_tier_expression(f"{{alias}}.{column}", output_alias)
        if expression is None:
            continue
        except_columns.append(column)
        outer_columns.append(expression)
        tier_columns.append(
            {"column": column, "output": output_alias, "expression": expression}
        )
        add_feature(
            output_alias,
            base_plan_id,
            _city_tier_expression(f"{alias}.{bare}", output_alias) or "",
            [{"alias": alias, "column": bare}],
            "LowCardinality(String)",
            {"kind": "preserve"},
        )

    # ---- pass-through columns (no derivation, carried for deployment) ----------------
    for column in wide:
        if column in {user_id, label} or column in except_columns:
            continue
        alias, bare = physical_ref(column)
        add_feature(
            column,
            base_plan_id,
            f"{alias}.{bare}",
            [{"alias": alias, "column": bare}],
            "Unknown",
            {"kind": "preserve"},
        )
    for dim in dim_plans:
        plan_id = f"{dim['table']}_scalar"
        for column in dim["columns"]:
            if column in dim["geo_columns"] or column == user_id:
                continue
            add_feature(
                column,
                plan_id,
                f"{dim['alias']}.{column}",
                [{"alias": dim["alias"], "column": column}],
                "Unknown",
                {"kind": "preserve"},
            )
        for item in dim["tier_outputs"]:
            add_feature(
                item["output"],
                plan_id,
                str(item["expression"]).rsplit(" AS ", 1)[0],
                [{"alias": dim["alias"], "column": item["column"]}],
                "LowCardinality(String)",
                {"kind": "preserve"},
            )

    except_columns = list(dict.fromkeys(except_columns))

    # A 1:N String column reaches the wide table as ``max(<alias>.<col>)`` -> ``String``.
    # The validation allow-list only covers *physical* String columns, so any such derived
    # column left unconverted is blocked by the warehouse high-cardinality gate. Record the
    # gap here so the local gate can fail fast instead of after a full aggregation run.
    unconverted_required_casts: list[str] = []
    for table_plan in table_plans:
        for plan_column in table_plan.columns:
            if plan_column.kind != "low_card_string":
                continue
            wide_column = f"{table_plan.table}_{plan_column.name}"
            if wide_column in string_plan:
                continue
            unconverted_required_casts.append(wide_column)

    # ---- outer CAST for sampled String columns the CTEs produce -------------------------
    # A 1:N String column is aggregated as ``max(<alias>.<col>)`` inside its CTE, which the
    # 2f gate requires verbatim. A sampled CAST rule therefore replaces the aggregated
    # column in the *outermost* projection, where the bar-alias is finally addressable.
    outer_except: list[str] = []
    cte_owners: dict[str, tuple] = {}
    for table_plan in table_plans:
        for plan_column in table_plan.columns:
            cte_owners[f"{table_plan.table}_{plan_column.name}"] = (table_plan, plan_column.name)

    for column, (table_plan, bare) in cte_owners.items():
        rule = string_plan.get(column)
        if not rule or column in geo_owned:
            continue
        cte_alias = table_plan.alias
        expressions = _render_string_cast(column, rule, policy)
        if not expressions:
            continue
        outer_except.append(column)
        outer_columns.extend(expressions)
        cast_columns.append({"column": column, "rule": rule, "scope": "outer"})
        if rule == "comma_list":
            for index, (suffix, output_type) in enumerate(
                (("_list_length", "UInt64"), ("_is_empty", "UInt8"))
            ):
                contract_expression = (
                    expressions[index]
                    .rsplit(" AS ", 1)[0]
                    .replace(f"{{alias}}.{column}", f"{cte_alias}.{bare}")
                    .replace("{alias}", cte_alias)
                )
                add_feature(
                    f"{column}{suffix}",
                    f"{table_plan.table}_by_user",
                    contract_expression,
                    [{"alias": cte_alias, "column": bare}],
                    output_type,
                    {"kind": "preserve"},
                )
            continue
        spec = (policy.get("string_cast_rules") or {}).get(
            "low_cardinality" if rule == "lowcard" else rule
        ) or {}
        contract_expression = (
            str(spec.get("expression") or "{col}")
            .replace("{col}", f"{cte_alias}.{bare}")
            .rsplit(" AS ", 1)[0]
        )
        add_feature(
            column,
            f"{table_plan.table}_by_user",
            contract_expression,
            [{"alias": cte_alias, "column": bare}],
            str(spec.get("output_type") or "LowCardinality(String)"),
            dict(spec.get("null_policy") or {"kind": "preserve"}),
        )

    # ---- 1:N CTE feature records (same expressions `_render_cte` emits) ----------------
    for table_plan in table_plans:
        plan_id = f"{table_plan.table}_by_user"
        alias = table_plan.alias
        for column in table_plan.columns:
            wide_column = f"{table_plan.table}_{column.name}"
            # When a sampled CAST replaces the aggregated String, skip the raw max(...)
            # feature so the contract keeps the converted expression only.
            cast_rule = string_plan.get(wide_column)
            if cast_rule and _render_string_cast(wide_column, cast_rule, policy):
                continue
            add_feature(
                wide_column,
                plan_id,
                _aggregate_expression(column, alias),
                [{"alias": alias, "column": column.name}],
                _output_type_for(column.kind, column.data_type, policy),
                dict(column.null_policy or {"kind": "preserve"}),
            )
        for field_plan in table_plan.list_fields:
            for term, slug in field_plan.suffixes:
                add_feature(
                    f"{table_plan.table}_{field_plan.column}_term_{slug}",
                    plan_id,
                    _term_expression(alias, field_plan.column, field_plan.delimiter, term, True),
                    [{"alias": alias, "column": field_plan.column}],
                    "UInt8",
                    {"kind": "preserve"},
                )
            add_feature(
                f"{table_plan.table}_{field_plan.column}_list_length",
                plan_id,
                _list_length_expression(alias, field_plan.column, field_plan.delimiter, True),
                [{"alias": alias, "column": field_plan.column}],
                "UInt64",
                {"kind": "preserve"},
            )
            add_feature(
                f"{table_plan.table}_{field_plan.column}_is_empty",
                plan_id,
                _list_empty_expression(alias, field_plan.column, True),
                [{"alias": alias, "column": field_plan.column}],
                "UInt8",
                {"kind": "preserve"},
            )
        add_feature(
            f"{table_plan.table}_row_count",
            plan_id,
            "count()",
            [],
            "UInt64",
            {"kind": "fill", "value": 0},
        )

    output_columns: list[str] = []
    for table_plan in table_plans:
        output_columns.extend(
            f"{table_plan.table}_{column.name}" for column in table_plan.columns
        )
        for field_plan in table_plan.list_fields:
            output_columns.extend(
                f"{table_plan.table}_{field_plan.column}_term_{slug}"
                for _term, slug in field_plan.suffixes
            )
            output_columns.append(f"{table_plan.table}_{field_plan.column}_list_length")
            output_columns.append(f"{table_plan.table}_{field_plan.column}_is_empty")
        output_columns.append(f"{table_plan.table}_row_count")
    for dim in dim_plans:
        output_columns.extend(
            column for column in dim["columns"] if column not in dim["geo_columns"]
        )
        output_columns.extend(item["output"] for item in dim["tier_outputs"])
    for column in wide:
        if column not in except_columns:
            output_columns.append(column)
    for expression in outer_columns:
        match = re.search(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", expression)
        if match:
            output_columns.append(match.group(1))
    output_columns = list(dict.fromkeys(output_columns))

    # EXCEPT alias bug workaround: move base-table EXCEPT columns to the outermost
    # projection so outer has()/CAST expressions can still see the source columns
    # (peeling them at ``w.*`` would yield Code 47 Missing columns).
    outer_except.extend(except_columns)
    except_columns.clear()

    body = _render_body(
        table_plans,
        dim_plans,
        output_database,
        user_id,
        except_columns,
        outer_columns,
        outer_except,
    )

    aggregation_sql = _replace_outside_comments(
        TEMPLATE_AGGREGATION.read_text(encoding="utf-8"),
        {"output_database": output_database, "user_id": user_id},
    )
    aggregation_sql = _replace_blocks_outside_comments(
        aggregation_sql, {AGGREGATION_BODY_BLOCK: body}
    )

    validation_sql = _replace_outside_comments(
        TEMPLATE_VALIDATION.read_text(encoding="utf-8"),
        {
            "output_database": output_database,
            "user_id": user_id,
            "label": label,
            "user_table": base_table,
        },
    )
    validation_sql = _replace_blocks_outside_comments(
        validation_sql,
        {
            LIST_FIELD_NAMES_BLOCK: ", ".join(f"'{name}'" for name in list_fields),
            GEO_MATCH_BLOCK: _geo_match_sql(),
            ALLOWED_STRING_BLOCK: ", ".join(
                f"'{name}'" for name in _allowed_string_columns(columns_by_table)
            ),
        },
    )

    plan = {
        "output_table": "step2_2_wide_aggregated",
        "input_table": "step2_1_wide_joined",
        "user_id": user_id,
        "base_table": base_table,
        "list_fields": list_fields,
        "list_owner": list_field_table,
        "one_to_one_list_fields": listed_1to1,
        "except_columns": list(dict.fromkeys(except_columns + outer_except)),
        "cast_columns": cast_columns,
        "unconverted_required_casts": unconverted_required_casts,
        "tier_columns": tier_columns,
        "dimension_tables": [item["table"] for item in dim_plans],
        "dimension_plans": [
            {
                "table": item["table"],
                "alias": item["alias"],
                "columns": item["columns"],
                "geo_columns": item["geo_columns"],
                "rows": item.get("rows"),
                "row_limit": item.get("row_limit"),
            }
            for item in dim_plans
        ],
        "output_columns": output_columns,
        "aggregations": [
            {
                "table": item.table,
                "cte": item.cte_name,
                "alias": item.alias,
                "user_key": item.user_key,
                "columns": [
                    {
                        "column": column.name,
                        "type": column.data_type,
                        "kind": column.kind,
                        "aggregation": column.aggregation,
                    }
                    for column in item.columns
                ],
                "dropped": item.dropped,
                "list_fields": [
                    {
                        "column": field_plan.column,
                        "delimiter": field_plan.delimiter,
                        "terms": [
                            {"term": term, "suffix": slug}
                            for term, slug in field_plan.suffixes
                        ],
                    }
                    for field_plan in item.list_fields
                ],
            }
            for item in table_plans
        ],
    }

    contract = _build_contract(
        table_plans,
        dim_plans,
        features,
        base_plan_id,
        base_table,
        user_id,
        label,
        columns_by_table,
        one_to_one,
        aliases,
    )

    derivation_md = _render_derivation(
        schema, table_plans, listed_1to1, cast_columns, tier_columns, dim_plans, policy
    )

    return ExpandResult(
        aggregation_sql=aggregation_sql,
        validation_sql=validation_sql,
        plan=plan,
        contract=contract,
        derivation_md=derivation_md,
    )


def _profile_for_wide(
    wide_column: str,
    base_table: str,
    one_to_one: list[dict[str, Any]],
    columns_by_table: dict[str, list[dict[str, str]]],
    profile_by_table: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any] | None:
    if wide_column in (profile_by_table.get(base_table) or {}):
        return (profile_by_table.get(base_table) or {}).get(wide_column)
    for table in one_to_one:
        name = str(table.get("table_name") or "").strip()
        if name == base_table or not name:
            continue
        prefix = f"{name}_"
        if wide_column.startswith(prefix):
            bare = wide_column[len(prefix) :]
            record = (profile_by_table.get(name) or {}).get(bare)
            if record is not None:
                return record
    return None


def _geo_columns(names: list[str]) -> list[str]:
    try:
        from step2_2_city_tier_sql import is_raw_city_column  # type: ignore
    except Exception:
        return []
    return [name for name in names if is_raw_city_column(name)]


def _geo_match_sql() -> str:
    """Boolean SQL matching raw geo columns, single-sourced from ``city_tier_sql``.

    The import is a hard requirement: a silently different fallback pattern would let raw
    geo columns through (or block legitimate ones) while the gate still reported PASS.
    """
    from step2_2_city_tier_sql import geo_column_match_sql  # type: ignore

    return geo_column_match_sql()


def _allowed_string_columns(columns_by_table: dict[str, list[dict[str, str]]]) -> list[str]:
    """Every physical ``String`` column — the allow-list for the high-cardinality gate.

    These are carried through step2_2 untouched (1:1 / dimension attributes are not
    converted), so ``name NOT IN (<this list>)`` must not flag them. The user key and the
    label are naturally covered when they are ``String``-typed; when they are not, they
    cannot trigger a String check anyway — so no special-casing is needed.

    Everything is derived from the physical schema, so derivation-only String columns
    produced inside the wide table are never on the list and must be sampled + CAST via
    ``string_cast_plan.txt``.
    """
    allowed: list[str] = []
    for columns in columns_by_table.values():
        for column in columns:
            base = _base_type(_strip_nullable(column["type"])[0])
            if base in {"String", "FixedString"}:
                allowed.append(column["name"])
    return list(dict.fromkeys(allowed))


def _city_tier_expression(qualified_column: str, output_alias: str) -> str | None:
    """Delegate the city-tier CASE body to the single-sourced city-tier script."""
    try:
        from step2_2_city_tier_sql import _load_mapping, build_city_tier_sql  # type: ignore
    except Exception:
        return None
    mapping_path = SCRIPTS_DIR.parent / "references" / "city_tier_map.json"
    try:
        mapping = _load_mapping(mapping_path)
    except Exception:
        return None
    return build_city_tier_sql(qualified_column, output_alias, mapping)


def _build_contract(
    table_plans: list[TablePlan],
    dim_plans: list[dict[str, Any]],
    features: dict[str, dict[str, Any]],
    base_plan_id: str,
    base_table: str,
    user_id: str,
    label: str,
    columns_by_table: dict[str, list[dict[str, str]]],
    one_to_one: list[dict[str, Any]],
    aliases: list[str],
) -> dict[str, Any]:
    """Serialize the feature records rendered alongside the SQL into the contract.

    ``features`` is built by ``expand()`` at the same moment each SQL expression is
    emitted, so this function re-derives nothing -- it only declares the relation plans
    the records reference.
    """
    join_tables: list[str] = []
    for spec in features.values():
        if spec["relation_plan"] != base_plan_id:
            continue
        for source in spec["source_columns"]:
            alias = str(source.get("alias") or "")
            if alias and alias != base_table and alias not in join_tables:
                join_tables.append(alias)

    joins: list[dict[str, str]] = []
    for table in join_tables:
        table_columns = [column["name"] for column in columns_by_table.get(table, [])]
        source_table = next(
            (item for item in one_to_one if str(item.get("table_name") or "").strip() == table),
            {"table_name": table},
        )
        key = _resolve_user_key(source_table, table_columns, aliases) or user_id
        joins.append(
            {
                "type": "LEFT",
                "table": table,
                "alias": table,
                "on": f"{base_table}.{user_id} = {table}.{key}",
            }
        )

    plans: dict[str, Any] = {
        base_plan_id: {
            "kind": "entity",
            "source": {"table": base_table, "alias": base_table},
            "joins": joins,
            "filters": [],
            "entity_key_expression": f"{base_table}.{user_id}",
        }
    }
    for table_plan in table_plans:
        plans[f"{table_plan.table}_by_user"] = {
            "kind": "user_aggregation",
            "source": {"table": table_plan.table, "alias": table_plan.alias},
            "joins": [],
            "filters": [],
            "entity_key_expression": f"{table_plan.alias}.{table_plan.user_key}",
        }
    for dim in dim_plans:
        plans[f"{dim['table']}_scalar"] = {
            "kind": "scalar",
            "source": {"table": dim["table"], "alias": dim["alias"]},
            "joins": [],
            "filters": [],
        }

    # Drop relation plans that no feature references (fully-dropped 1:N tables /
    # geo-only dimension tables whose only columns were EXCEPTed).
    referenced = {str(spec.get("relation_plan") or "") for spec in features.values()}
    plans = {
        plan_id: plan
        for plan_id, plan in plans.items()
        if plan_id in referenced or plan_id == base_plan_id
    }

    return {
        "contract_version": 1,
        "source_artifact": "step2_2_feature_aggregation_expanded.sql",
        "entity": {
            "grain": "user",
            "base_relation_plan": base_plan_id,
            "entity_key": user_id,
            "label_column": label,
        },
        "relation_plans": plans,
        "features": features,
        "generated_by": "step2_2_expand_sql.py",
    }


def _render_derivation(
    schema: dict[str, Any],
    table_plans: list[TablePlan],
    listed_1to1: list[dict[str, Any]],
    cast_columns: list[dict[str, str]],
    tier_columns: list[dict[str, str]],
    dim_plans: list[dict[str, str]],
    policy: dict[str, Any],
) -> str:
    lines: list[str] = [
        "# step2_2 特征衍生说明",
        "",
        "本文档按 `references/aggregation_policy.json` 确定性生成，与 "
        "`step2_2_feature_aggregation_expanded.sql` 出自同一份特征定义；禁止手工编辑。",
        "",
        "## 1:N 聚合",
        "",
    ]
    if not table_plans:
        lines.append("本次无 1:N 行为表。")
        lines.append("")
    for table_plan in table_plans:
        lines.append(f"### {table_plan.table}（{table_plan.cte_name}）")
        lines.append("")
        lines.append("| 源列 | 类型 | 聚合 | 产出列 |")
        lines.append("|------|------|------|--------|")
        for column in table_plan.columns:
            lines.append(
                f"| {column.name} | {column.data_type} | {column.aggregation} "
                f"| {table_plan.table}_{column.name} |"
            )
        lines.append(f"| — | — | count | {table_plan.table}_row_count |")
        for field_plan in table_plan.list_fields:
            for term, slug in field_plan.suffixes:
                lines.append(
                    f"| {field_plan.column} | list:{field_plan.delimiter} | 二元展开 "
                    f"| {table_plan.table}_{field_plan.column}_term_{slug}（{term}） |"
                )
            lines.append(
                f"| {field_plan.column} | list:{field_plan.delimiter} | length "
                f"| {table_plan.table}_{field_plan.column}_list_length |"
            )
            lines.append(
                f"| {field_plan.column} | list:{field_plan.delimiter} | 空判定 "
                f"| {table_plan.table}_{field_plan.column}_is_empty |"
            )
        lines.append("")
        if table_plan.dropped:
            lines.append("丢弃列：")
            lines.append("")
            for item in table_plan.dropped:
                reason = (policy.get("drop_reasons") or {}).get(item["reason"], item["reason"])
                lines.append(f"- `{item['column']}`：{item['reason']}（{reason}）")
            lines.append("")

    lines.append("## 1:1 表列表字段（最外层展开）")
    lines.append("")
    if not listed_1to1:
        lines.append("本次无 1:1 列表字段。")
        lines.append("")
    for item in listed_1to1:
        lines.append(f"### {item['wide_column']}（delimiter `{item['delimiter']}`）")
        lines.append("")
        lines.append("| 词项 | 列名后缀 |")
        lines.append("|------|----------|")
        for entry in item["terms"]:
            lines.append(f"| {entry['term']} | {entry['suffix']} |")
        lines.append("")

    lines.append("## 残留 String 转换")
    lines.append("")
    if not cast_columns:
        lines.append("本次无采样转换列。")
        lines.append("")
    for item in cast_columns:
        lines.append(f"- `{item['column']}`：{item['rule']}")
    if cast_columns:
        lines.append("")

    lines.append("## 地理列分级")
    lines.append("")
    if not tier_columns:
        lines.append("本次无地理列。")
        lines.append("")
    for item in tier_columns:
        lines.append(f"- `{item['column']}` → `{item['output']}`（原列已从宽表 EXCEPT）")
    if tier_columns:
        lines.append("")

    lines.append("## 维度表拼接")
    lines.append("")
    if not dim_plans:
        lines.append("本次无维度表。")
        lines.append("")
    for dim in dim_plans:
        lines.append(f"- `{dim['table']}`（CROSS JOIN，别名 `{dim['alias']}`）")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# pass 1: term query
# --------------------------------------------------------------------------------------


_NUMERIC_RE_SQL = r"^[-+]?[0-9]+(\.[0-9]+)?$"
_STRING_SAMPLE_CONDITION_SQL = (
    "type IN ('String', 'Nullable(String)') OR "
    "(position(type, 'String') > 0 AND position(type, 'LowCardinality') = 0)"
)


def _physical_owner(
    wide_column: str,
    base_table: str,
    one_to_one: list[dict[str, Any]],
    columns_by_table: dict[str, list[dict[str, str]]],
) -> tuple[str, str]:
    """Map a wide-table spelling back to its physical ``(table, bare column)``."""
    if wide_column in {column["name"] for column in columns_by_table.get(base_table, [])}:
        return base_table, wide_column
    for candidate in one_to_one:
        name = str(candidate.get("table_name") or "").strip()
        if not name or name == base_table:
            continue
        prefix = f"{name}_"
        if wide_column.startswith(prefix):
            bare = wide_column[len(prefix) :]
            if bare in {column["name"] for column in columns_by_table.get(name, [])}:
                return name, bare
    return base_table, wide_column


def string_cast_candidates(
    schema: dict[str, Any],
    meta: dict[str, Any],
    dump: dict[str, Any],
    list_fields: list[str],
    policy: dict[str, Any],
) -> list[dict[str, str]]:
    """Enumerate every wide-table column that may need a sampled String CAST.

    Two populations, told apart by ``requirement``:

    * ``required`` -- String columns the 1:N aggregation CTEs produce (``max(String)``).
      They are absent from the validation allow-list, so if they survive as ``String`` the
      high-cardinality gate blocks. They must be sampled and given a cast rule.
    * ``optional`` -- physical ``String`` columns carried through step2_1 untouched. The
      validation allow-list lets them stay ``String``, so casting them stays a judgement
      call rather than a requirement.

    Columns already handled elsewhere are excluded: delimiter list fields (splitByChar
    branch) and geographic columns (``{col}_tier`` branch, decided by name, not sampling).
    """
    base_table = str(schema.get("user_table") or "").strip()
    columns_by_table = _columns_by_table(meta)
    profile_by_table = _profile_by_table(dump)
    totals = _table_rows(dump)
    aliases = _user_key_columns(meta, schema)
    one_to_one, one_to_many, _dims = _classification_buckets(schema)
    wide = _wide_columns(schema, columns_by_table, meta)

    list_field_table: dict[str, str] = {}
    for table_name, columns in profile_by_table.items():
        for column_name in columns:
            if column_name in list_fields and column_name not in list_field_table:
                list_field_table[column_name] = table_name

    def wide_spelling(owner: str, column: str) -> str:
        return column if owner == base_table else f"{owner}_{column}"

    list_spellings = {
        wide_spelling(list_field_table[field], field)
        for field in list_fields
        if field in list_field_table
    }
    geo = set(_geo_columns(wide))

    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    # 1:N CTE outputs: a kept low-cardinality String column is aggregated into a String.
    for table in one_to_many:
        name = str(table.get("table_name") or "").strip()
        columns = columns_by_table.get(name) or []
        if not name or not columns:
            continue
        plain = [column["name"] for column in columns]
        key = _resolve_user_key(table, plain, aliases)
        if not key:
            continue
        kept, _dropped = _plan_table_columns(
            name,
            columns,
            profile_by_table.get(name) or {},
            totals.get(name),
            key,
            policy,
            {field for field in list_fields if list_field_table.get(field) == name},
        )
        for column_plan in kept:
            if column_plan.kind != "low_card_string":
                continue
            wide_column = f"{name}_{column_plan.name}"
            if wide_column in seen:
                continue
            seen.add(wide_column)
            candidates.append(
                {
                    "wide_column": wide_column,
                    "source_table": name,
                    "source_column": column_plan.name,
                    "origin": "derived_1toN",
                    "requirement": "required",
                }
            )

    # Physical 1:1 / base String columns carried through untouched.
    for column in wide:
        if column in seen or column in list_spellings or column in geo:
            continue
        record = _profile_for_wide(column, base_table, one_to_one, columns_by_table, profile_by_table)
        if _classify_type(str((record or {}).get("data_type") or ""), policy) != "low_card_string":
            continue
        owner, bare = _physical_owner(column, base_table, one_to_one, columns_by_table)
        candidates.append(
            {
                "wide_column": column,
                "source_table": owner,
                "source_column": bare,
                "origin": "physical",
                "requirement": "optional",
            }
        )
    return candidates


def build_string_sample_query(
    schema: dict[str, Any],
    candidates: list[dict[str, str]],
    policy: dict[str, Any],
) -> str:
    """One ``SELECT`` sampling every candidate column (200 rows each, aggregated)."""
    output_database = str(schema.get("output_database") or "").strip()
    if not output_database:
        raise SystemExit("schema_resolution.output_database 为空，无法填充 {{output_database}}")
    limit = int(policy.get("string_sample_limit") or 200)
    branches: list[str] = []
    for candidate in candidates:
        table = candidate["source_table"]
        column = candidate["source_column"]
        branches.append(
            "(\n"
            f"    SELECT {_quote(table)} AS source_table, {_quote(column)} AS source_column,\n"
            "        count() AS n_sample,\n"
            "        uniqExact(val) AS n_unique_sample,\n"
            f"        countIf(match(trimBoth(val), {_quote(_NUMERIC_RE_SQL)})) AS n_numeric,\n"
            "        countIf(position(val, ',') > 0) AS n_has_comma,\n"
            "        countIf(parseDateTimeBestEffortOrNull(val) IS NOT NULL) AS n_datetime\n"
            "    FROM\n"
            "    (\n"
            f"        SELECT toString(assumeNotNull({column})) AS val\n"
            f"        FROM {output_database}.{table}\n"
            f"        WHERE {column} IS NOT NULL AND toString({column}) != ''\n"
            f"        LIMIT {limit}\n"
            "    )\n"
            ")"
        )
    if not branches:
        raise SystemExit("没有需要采样的 String 候选列；跳过本趟，finalize 无需 --string-plan。")
    return "SELECT *\nFROM\n(\n" + "\nUNION ALL\n".join(branches) + "\n);"


def string_cast_rule_from_sample(
    column: str,
    row: dict[str, Any],
    policy: dict[str, Any],
) -> str:
    """Deterministic sample -> rule mapping. Thresholds come from ``aggregation_policy.json``."""
    try:
        from step2_2_city_tier_sql import is_raw_city_column  # type: ignore
    except Exception:  # pragma: no cover - city_tier_sql ships with the skill
        is_raw_city_column = lambda _name: False  # type: ignore[assignment]

    if is_raw_city_column(column):
        return "city_tier"
    n_sample = int(row.get("n_sample") or 0)
    if n_sample <= 0:
        return "lowcard"
    numeric = int(row.get("n_numeric") or 0) / n_sample
    comma = int(row.get("n_has_comma") or 0) / n_sample
    datetime_hits = int(row.get("n_datetime") or 0)
    if numeric >= float(policy["string_numeric_ratio"]):
        return "numeric"
    if comma >= float(policy["string_comma_ratio"]):
        return "comma_list"
    if datetime_hits >= n_sample:
        return "datetime"
    return "lowcard"


def derive_string_plan(
    candidates: list[dict[str, str]],
    sample_rows: list[dict[str, Any]],
    policy: dict[str, Any],
) -> tuple[dict[str, str], list[str]]:
    """Turn raw sample rows into ``string_cast_plan.txt`` entries plus operator notes."""
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in sample_rows:
        table = str(row.get("source_table") or "").strip()
        column = str(row.get("source_column") or "").strip()
        if table and column:
            by_key[(table, column)] = row

    plan: dict[str, str] = {}
    notes: list[str] = []
    for candidate in candidates:
        key = (candidate["source_table"], candidate["source_column"])
        row = by_key.get(key)
        if row is None:
            if candidate["requirement"] == "required":
                notes.append(
                    f"{candidate['wide_column']}：缺采样结果，必须补采后转换"
                    f"（1:N 派生 String 列，留在宽表会触发高基数门禁）"
                )
            continue
        rule = string_cast_rule_from_sample(candidate["wide_column"], row, policy)
        plan[candidate["wide_column"]] = rule
        n_sample = int(row.get("n_sample") or 0)
        numeric = int(row.get("n_numeric") or 0)
        comma = int(row.get("n_has_comma") or 0)
        notes.append(
            f"{candidate['wide_column']} -> {rule}"
            f"（样本 {n_sample}，numeric {numeric}，comma {comma}，"
            f"{'必转' if candidate['requirement'] == 'required' else '可选'}）"
        )
    return plan, notes


def _term_branches(
    schema: dict[str, Any],
    dump: dict[str, Any],
    list_fields: list[str],
    policy: dict[str, Any],
) -> list[str]:
    """One parenthesized SELECT branch per delimiter list field."""
    output_database = str(schema.get("output_database") or "").strip()
    base_table = str(schema.get("user_table") or "").strip()
    profile_by_table = _profile_by_table(dump)
    fetch = int(policy.get("term_fetch_limit") or 101)

    owner: dict[str, str] = {}
    for table_name, columns in profile_by_table.items():
        for column_name in columns:
            if column_name in list_fields and column_name not in owner:
                owner[column_name] = table_name

    branches: list[str] = []
    for field_name in list_fields:
        table_name = owner.get(field_name)
        if not table_name:
            raise SystemExit(
                f"list_fields.txt 中的字段 {field_name} 在 step2_0_profile_dump.json 里找不到所属表"
            )
        record = (profile_by_table.get(table_name) or {}).get(field_name) or {}
        delimiter = _delimiter_of(record.get("sample_values"))
        if not delimiter:
            continue
        if table_name == base_table:
            source = f"{output_database}.step2_1_wide_joined"
        else:
            source = f"{output_database}.{table_name}"
        branches.append(
            f"(SELECT {_quote(table_name)} AS table_name, {_quote(field_name)} AS column_name, "
            f"term, count() AS freq\n"
            f"FROM {source}\n"
            f"ARRAY JOIN splitByChar({_quote(delimiter)}, assumeNotNull({field_name})) AS term\n"
            f"WHERE term != ''\n"
            f"GROUP BY term\n"
            f"ORDER BY freq DESC\n"
            f"LIMIT {fetch})"
        )
    return branches


def _term_query_from_branches(branches: list[str]) -> str:
    if not branches:
        raise SystemExit(
            "list_fields.txt 中没有任何带 delimiter 的字段，无需查询词项频次；"
            "跳过本趟，直接运行 finalize（无需 --raw-terms）。"
        )
    return "SELECT *\nFROM\n(\n" + "\nUNION ALL\n".join(branches) + "\n);"


def build_term_query(
    schema: dict[str, Any],
    dump: dict[str, Any],
    list_fields: list[str],
    policy: dict[str, Any],
) -> str:
    """Single UNION ALL term-frequency query (all delimiter fields)."""
    return _term_query_from_branches(_term_branches(schema, dump, list_fields, policy))


def build_term_queries(
    schema: dict[str, Any],
    dump: dict[str, Any],
    list_fields: list[str],
    policy: dict[str, Any],
) -> list[str]:
    """Split term-frequency queries by ``term_query_batch_size`` (MCP payload cap)."""
    branches = _term_branches(schema, dump, list_fields, policy)
    if not branches:
        raise SystemExit(
            "list_fields.txt 中没有任何带 delimiter 的字段，无需查询词项频次；"
            "跳过本趟，直接运行 finalize（无需 --raw-terms）。"
        )
    batch_size = int(policy.get("term_query_batch_size") or 1)
    if batch_size <= 0:
        batch_size = 1
    queries: list[str] = []
    for index in range(0, len(branches), batch_size):
        queries.append(_term_query_from_branches(branches[index : index + batch_size]))
    return queries
