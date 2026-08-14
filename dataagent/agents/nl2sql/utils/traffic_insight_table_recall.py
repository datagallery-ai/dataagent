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
"""Traffic Insight table recall: column EQ soft/hard select, dim* filter, family, soft coverage."""

from __future__ import annotations

import re
from typing import Any

from dataagent.agents.nl2sql.utils.traffic_insight_field_normalizer import split_fields_by_catalog
from dataagent.utils.log import logger

_GRANULARITY_RE = re.compile(r"_(?P<granularity>\d+(?:min|h|d|w|m))$", re.IGNORECASE)
_DEFAULT_EXCLUDE_PREFIXES = ("dim",)


def bare_table_name(table_name: str) -> str:
    """Strip optional ``db.`` prefix from a table identifier."""
    name = str(table_name or "").strip()
    if "." in name:
        return name.rsplit(".", 1)[-1]
    return name


def qualify_table_name(table_name: str, database_name: str | None = None) -> str:
    """Return ``db.table`` when database is known and table is bare."""
    name = str(table_name or "").strip()
    if not name:
        return name
    if "." in name or not database_name:
        return name
    return f"{database_name}.{name}"


def is_excluded_dimension_table(table_name: str, prefixes: tuple[str, ...] | list[str] | None = None) -> bool:
    """Return True when bare table name starts with a configured exclude prefix (e.g. dim)."""
    bare = bare_table_name(table_name).casefold()
    return any(bare.startswith(str(prefix).casefold()) for prefix in prefixes or _DEFAULT_EXCLUDE_PREFIXES)


def parse_traffic_insight_table_name(table_name: str) -> dict[str, str] | None:
    """Parse ``{stem}_{granularity}`` fact table names; return None if no granularity suffix."""
    bare = bare_table_name(table_name)
    match = _GRANULARITY_RE.search(bare)
    if not match:
        return None
    granularity = match.group("granularity").lower()
    family_name = bare[: match.start()]
    if not family_name:
        return None
    return {
        "bare_table_name": bare,
        "family_name": family_name,
        "granularity": granularity,
        "qualified_hint": str(table_name or "").strip(),
    }


def extract_tables_from_column_search(
    payload: Any,
    *,
    database_name: str | None = None,
) -> set[str]:
    """Collect ``db.table`` (or bare table) ids from a search/basic data_column result."""
    tables: set[str] = set()
    if not isinstance(payload, dict):
        return tables
    entities = payload.get("entities")
    if not isinstance(entities, list):
        return tables

    db_filter = str(database_name or "").strip()
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        attrs = entity.get("attributes")
        if not isinstance(attrs, dict):
            attrs = {}
        table = str(attrs.get("table_name_en") or attrs.get("table_name") or "").strip()
        if not table:
            continue
        db = str(attrs.get("db_name_en") or attrs.get("db_name") or "").strip()
        if db_filter and db and db != db_filter:
            continue
        if db_filter and not db:
            tables.add(qualify_table_name(table, db_filter))
        elif db:
            tables.add(f"{db}.{bare_table_name(table)}")
        else:
            tables.add(bare_table_name(table))
    return tables


def select_tables_from_field_hits(
    field_hits: dict[str, set[str]],
    *,
    need_d: set[str],
    need_m: set[str],
    exclude_prefixes: tuple[str, ...] | list[str] | None = None,
    max_candidate_tables: int = 200,
) -> tuple[list[str], str]:
    """Build table→fields index from per-field sets, then rank (test / offline helper)."""
    table_to_fields: dict[str, set[str]] = {}
    fields = sorted((need_d or set()) | (need_m or set()))
    for field in fields:
        for table in field_hits.get(field) or set():
            name = str(table or "").strip()
            if not name or is_excluded_dimension_table(name, exclude_prefixes):
                continue
            table_to_fields.setdefault(name, set()).add(field)
    return rank_tables_from_field_index(
        table_to_fields,
        need_field_count=len(fields),
        max_candidate_tables=max_candidate_tables,
    )


def add_tables_to_field_index(
    table_to_fields: dict[str, set[str]],
    *,
    field: str,
    tables: set[str] | list[str],
    exclude_prefixes: tuple[str, ...] | list[str] | None = None,
) -> int:
    """Stream one page (or batch) of tables into ``table → {fields}``; return newly-or-already indexed count."""
    indexed = 0
    for raw in tables:
        name = str(raw or "").strip()
        if not name or is_excluded_dimension_table(name, exclude_prefixes):
            continue
        table_to_fields.setdefault(name, set()).add(field)
        indexed += 1
    return indexed


def rank_tables_from_field_index(
    table_to_fields: dict[str, set[str]],
    *,
    need_field_count: int,
    max_candidate_tables: int = 200,
) -> tuple[list[str], str]:
    """Rank tables by |hit fields| descending, then apply ``max_candidate_tables``."""
    if need_field_count <= 0:
        return [], "empty_need_fields"

    hit_count = {table: len(fields) for table, fields in table_to_fields.items() if fields}
    if not hit_count:
        logger.info("Traffic Insight perceptor step=field_hit_count_rank produced no tables")
        return [], "field_hit_count_rank"

    ordered = sorted(hit_count.keys(), key=lambda name: (-hit_count[name], name))
    max_hits = hit_count[ordered[0]]
    if max_candidate_tables > 0 and len(ordered) > max_candidate_tables:
        logger.info(
            "Traffic Insight perceptor step=field_hit_count_rank truncated from {} to max_candidate_tables={}",
            len(ordered),
            max_candidate_tables,
        )
        ordered = ordered[:max_candidate_tables]

    logger.info(
        "Traffic Insight perceptor step=field_hit_count_rank need_fields={} max_hits={}/{} tables={}",
        need_field_count,
        max_hits,
        need_field_count,
        len(ordered),
    )
    return ordered, "field_hit_count_rank"


def build_families_from_tables(
    table_names: list[str],
    *,
    exclude_prefixes: tuple[str, ...] | list[str] | None = None,
) -> list[dict[str, Any]]:
    """Group physical tables into families by stripping granularity suffixes.

    Every family keeps one representative table. Callers must fetch hybrid columns for
    **all** representatives; do not truncate families before that step.
    """
    grouped: dict[str, dict[str, str]] = {}
    for raw in table_names:
        parsed = parse_traffic_insight_table_name(raw)
        if not parsed:
            logger.warning("Traffic Insight table recall skipped unparsable table: {}", raw)
            continue
        if is_excluded_dimension_table(parsed["bare_table_name"], exclude_prefixes):
            continue
        family_name = parsed["family_name"]
        bucket = grouped.setdefault(family_name, {})
        # Prefer first-seen qualified name; granularity → bare or qualified table
        bucket[parsed["granularity"]] = (
            parsed["qualified_hint"] if "." in parsed["qualified_hint"] else parsed["bare_table_name"]
        )

    families: list[dict[str, Any]] = []
    for family_name, gran_to_table in sorted(grouped.items()):
        pairs = sorted(gran_to_table.items(), key=lambda item: _granularity_sort_key(item[0]))
        families.append(
            {
                "family_name": family_name,
                "available_granularities": [granularity for granularity, _ in pairs],
                "candidate_table_names": [table for _, table in pairs],
                "dimensions": [],
                "metrics": [],
                "representative_table": pairs[0][1],
            }
        )
    return families


def tables_missing_from_hybrid_columns(
    requested_tables: list[str],
    table_columns: dict[str, dict[str, Any]],
) -> list[str]:
    """Return requested table ids that have no entry in a hybrid columns map."""
    missing: list[str] = []
    for raw in requested_tables:
        name = str(raw or "").strip()
        if not name:
            continue
        if name in table_columns or bare_table_name(name) in table_columns:
            continue
        missing.append(name)
    return missing


def parse_hybrid_table_columns(payload: Any) -> dict[str, dict[str, Any]]:
    """Parse hybrid/table-columns response into ``{bare_or_qualified: {description, columns}}``.

    Column map value: ``{name: {description, value_type}}``.
    """
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(payload, list):
        return result
    for item in payload:
        if not isinstance(item, dict):
            continue
        table = str(item.get("table") or "").strip()
        if not table:
            continue
        db = str(item.get("db") or "").strip()
        qualified = f"{db}.{table}" if db else table
        columns: dict[str, dict[str, str]] = {}
        for col in item.get("columns") or []:
            if not isinstance(col, dict):
                continue
            col_name = str(col.get("columnNameEn") or col.get("column_name_en") or "").strip()
            if not col_name:
                continue
            columns[col_name] = {
                "description": str(col.get("description") or "").strip(),
                "value_type": str(col.get("valueType") or col.get("value_type") or "").strip(),
                "example_values": "",
            }
        meta = {
            "description": str(item.get("description") or "").strip(),
            "columns": columns,
            "qualified_name": qualified,
            "bare_name": bare_table_name(table),
        }
        result[qualified] = meta
        result[bare_table_name(table)] = meta
    return result


def enrich_families_with_columns(
    families: list[dict[str, Any]],
    table_columns: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach catalog-split dimensions/metrics from hybrid columns.

    Callers must ensure hybrid columns cover every representative; missing meta is an error
    at the fetch layer, not a silent family drop here.
    """
    kept: list[dict[str, Any]] = []
    for family in families:
        rep = family["representative_table"]
        meta = table_columns.get(rep) or table_columns.get(bare_table_name(rep))
        if not meta:
            raise ValueError(f"hybrid columns missing for representative_table={rep}")
        col_names = set(meta["columns"])
        split = split_fields_by_catalog(col_names)
        enriched = dict(family)
        enriched["dimensions"] = sorted(split["dimensions"])
        enriched["metrics"] = sorted(split["metrics"])
        enriched["column_meta"] = meta
        enriched["column_names"] = col_names
        kept.append(enriched)
    return kept


def apply_coverage_filter(
    families: list[dict[str, Any]],
    table_columns: dict[str, dict[str, Any]],
    *,
    need_d: set[str],
    need_m: set[str],
) -> list[dict[str, Any]]:
    """Enrich families with hybrid columns; keep all (ranking is §6.4's job)."""
    return enrich_families_with_columns(families, table_columns)


def rank_and_truncate_families(
    families: list[dict[str, Any]],
    *,
    need_d: set[str],
    need_m: set[str],
    max_llm_table_families: int = 20,
) -> list[dict[str, Any]]:
    """Rank by verified column coverage of need fields, then fewer extras; LLM prompt limit."""
    needed = (need_d or set()) | (need_m or set())

    def sort_key(family: dict[str, Any]) -> tuple:
        dims = set(family.get("dimensions") or [])
        metrics = set(family.get("metrics") or [])
        col_names = family.get("column_names") or (dims | metrics)
        coverage = len(needed & set(col_names))
        extra_dims = len(dims - (need_d or set()))
        extra_metrics = len(metrics - (need_m or set()))
        return (-coverage, extra_dims, extra_metrics, family["family_name"])

    ordered = sorted(families, key=sort_key)
    if max_llm_table_families > 0 and len(ordered) > max_llm_table_families:
        logger.info(
            "Traffic Insight max_llm_table_families truncated sorted families from {} to {}",
            len(ordered),
            max_llm_table_families,
        )
        return ordered[:max_llm_table_families]
    return ordered


def resolve_family_selection(
    selection: dict[str, str] | None,
    families: list[dict[str, Any]],
) -> str | None:
    """Map LLM ``{family_name, granularity}`` to a physical table name."""
    if not selection:
        return None
    family_name = str(selection.get("family_name") or "").strip()
    if "." in family_name:
        family_name = family_name.rsplit(".", 1)[-1]
    granularity = str(selection.get("granularity") or "").strip().lower()
    if not family_name or not granularity:
        return None
    for family in families:
        if family["family_name"] != family_name:
            continue
        for gran, table_name in zip(
            family["available_granularities"],
            family["candidate_table_names"],
            strict=True,
        ):
            if gran == granularity:
                return table_name
    return None


def schema_from_hybrid_columns(
    table_name: str,
    table_columns: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build NL2SQL schema IR for a single table from hybrid column metadata."""
    meta = table_columns.get(table_name) or table_columns.get(bare_table_name(table_name))
    if not meta:
        return {}
    bare = bare_table_name(table_name)
    return {
        bare: {
            "description": meta.get("description") or "",
            "columns": {
                col: {
                    "description": info.get("description") or "",
                    "value_type": info.get("value_type") or "",
                    "example_values": info.get("example_values") or "",
                }
                for col, info in (meta.get("columns") or {}).items()
            },
        }
    }


def normalize_example_values(raw: str | None) -> str:
    """Normalize ``a:b|c:d`` style value descriptions to ``a=b|c=d`` for DDL."""
    if not raw:
        return ""
    return "|".join(item.replace(":", "=", 1).removesuffix("=") for item in str(raw).split("|") if item)


def example_values_by_column_from_columns_info(
    table_name: str,
    columns_info: dict[str, Any] | None,
) -> dict[str, str]:
    """Extract normalized ``example_values`` keyed by column name from ``table-columns-info``."""
    if not isinstance(columns_info, dict) or not columns_info:
        return {}
    bare = bare_table_name(table_name)
    want_qualified = table_name if "." in str(table_name) else None
    values: dict[str, str] = {}
    for dtc, meta in columns_info.items():
        if not isinstance(dtc, str) or not isinstance(meta, dict):
            continue
        parts = dtc.split(".", 2)
        if len(parts) != 3:
            continue
        db_name, tbl, col = parts
        qualified = f"{db_name}.{tbl}"
        if want_qualified:
            if qualified != want_qualified:
                continue
        elif tbl != bare:
            continue
        normalized = normalize_example_values(meta.get("value_description"))
        if normalized:
            values[col] = normalized
    return values


def enrich_schema_example_values_from_columns_info(
    schema: dict[str, Any],
    table_name: str,
    columns_info: dict[str, Any] | None,
) -> dict[str, Any]:
    """Fill empty ``example_values`` from ``table-columns-info``; never overwrite hybrid fields.

    - Table/column set, descriptions, and ``value_type`` stay as hybrid built them.
    - Only columns already present in ``schema`` may receive values.
    - Non-empty ``example_values`` already on a column are left unchanged.
    - Columns that appear only in ``columns_info`` are ignored (not added).
    """
    if not schema:
        return schema
    bare = bare_table_name(table_name)
    table_ir = schema.get(bare)
    if not isinstance(table_ir, dict):
        return schema
    columns = table_ir.get("columns")
    if not isinstance(columns, dict) or not columns:
        return schema
    value_by_col = example_values_by_column_from_columns_info(table_name, columns_info)
    if not value_by_col:
        return schema
    for col, info in columns.items():
        if not isinstance(info, dict):
            continue
        if str(info.get("example_values") or "").strip():
            continue
        supplement = value_by_col.get(col)
        if supplement:
            info["example_values"] = supplement
    return schema


def format_traffic_insight_table_family_prompt_context(families: list[dict[str, Any]]) -> str:
    """Render candidate families markdown for LLM₂."""
    dim_fields: list[str] = []
    for family in families:
        for field in family.get("dimensions") or []:
            if field not in dim_fields:
                dim_fields.append(field)

    lines = ["## 维度说明"]
    if dim_fields:
        for field in dim_fields:
            lines.append(f"- `{field}`")
    else:
        lines.append("- （候选表簇未携带维度列表）")

    lines.extend(["", "## 候选表簇"])
    for index, family in enumerate(families, start=1):
        dimensions = ", ".join(f"`{field}`" for field in family.get("dimensions") or []) or "（无）"
        metrics = ", ".join(f"`{field}`" for field in family.get("metrics") or []) or "（无）"
        granularities = ", ".join(f"`{value}`" for value in family.get("available_granularities") or [])
        lines.extend(
            [
                f"### {index}. `{family['family_name']}`",
                f"- 表簇包含维度：{dimensions}",
                f"- 表簇包含指标：{metrics}",
                f"- 表簇可用时间粒度：{granularities}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def _granularity_sort_key(granularity: str) -> tuple:
    text = str(granularity or "").lower()
    match = re.fullmatch(r"(\d+)(min|h|d|w|m)", text)
    if not match:
        return (999, 0, text)
    amount = int(match.group(1))
    unit = match.group(2)
    unit_order = {"min": 0, "h": 1, "d": 2, "w": 3, "m": 4}
    return (unit_order.get(unit, 9), amount, text)
