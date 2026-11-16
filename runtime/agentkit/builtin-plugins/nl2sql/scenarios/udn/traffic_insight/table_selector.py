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
"""traffic_insight table selection: field-EQ recall → family narrowing → one fact table.

Ported from the legacy ``traffic_insight_perceptor``. Recall / family / parsing
helpers live in :mod:`.data.traffic_insight_table_recall`; this class owns the
semantic-service calls (search/basic EQ pagination, hybrid table-columns
batches, table-columns-info value supplement) and the two LLM steps (field
extraction, family + granularity selection). Unlike business_twin there is no
deterministic fallback: a failed LLM family selection surfaces as an error.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger
from nl2sql.constants import DEFAULT_NL2SQL_SEMANTIC_TABLE_COLUMNS_LIMIT
from nl2sql.errors import DataAgentError
from nl2sql.hooks import HookContext, SchemaContext
from nl2sql.utils.nl2sql_utils import schema_to_ddl

from ..common.llm import execute_with_llm_json
from ..common.rules import current_date_rule, time_granularity_rules
from .data.traffic_insight_field_normalizer import (
    assert_need_fields,
    normalize_traffic_insight_fields,
)
from .data.traffic_insight_table_recall import (
    add_tables_to_field_index,
    build_families_from_tables,
    enrich_families_with_columns,
    enrich_schema_example_values_from_columns_info,
    extract_tables_from_column_search,
    format_traffic_insight_table_family_prompt_context,
    parse_hybrid_table_columns,
    qualify_table_name,
    resolve_family_selection,
    schema_from_hybrid_columns,
    select_families_by_max_need_hits,
    select_families_by_min_extra_fields_converged,
    tables_from_field_index,
    tables_missing_from_hybrid_columns,
)

if TYPE_CHECKING:
    from dataagent import SemanticServiceClient


def _scenario_error(message: str, detail: str = "") -> DataAgentError:
    """Map a legacy NL2SQLError(message, detail=...) onto the plugin's error type."""
    fact = f"{message}: {detail}" if detail else message
    return DataAgentError(source="internal", component="nl2sql", fact=fact)


class TrafficInsightTableSelector:
    """Selects one traffic-insight fact table and builds the run's SchemaContext."""

    def __init__(
        self,
        ctx: HookContext,
        *,
        base: Path,
        semantic_client: SemanticServiceClient | None,
        column_eq_page_size: int = 200,
        column_eq_max_offset: int = 100_000,
        hybrid_batch_size: int = 50,
        candidate_table_families_limit: int = 150,
        exclude_table_prefix: tuple[str, ...] | list[str] = ("dim",),
    ):
        self._column_eq_page_size = column_eq_page_size
        self._column_eq_max_offset = column_eq_max_offset
        self._exclude_prefixes = tuple(str(p) for p in (exclude_table_prefix or ("dim",)))
        self._hybrid_batch_size = hybrid_batch_size
        self._candidate_table_families_limit = candidate_table_families_limit
        self._model = ctx.model
        self._prompt_dir = base / "prompts"
        self._sql_rules = (base / "sql_rules.md").read_text(encoding="utf-8")
        self._db = ctx.config.get("database.db_id", "")
        self._semantic_client = semantic_client

    @property
    def semantic_client(self):
        """The assembly-bound semantic client; raises a config error on first use when absent."""
        if self._semantic_client is None:
            raise DataAgentError(
                source="config",
                component="nl2sql",
                fact="semantic 服务未配置（settings.semantic）：无法选表",
            )
        return self._semantic_client

    async def select(self, question: str) -> SchemaContext:
        """Run the selection pipeline for one question; errors propagate (no silent fallback)."""
        rules = self._sql_rules + current_date_rule()
        schema, joins, explicit_granularity = await self._schema_linking(question)
        if explicit_granularity:
            rules += time_granularity_rules()
        return SchemaContext(
            schema=schema,
            joins=joins,
            schema_str=schema_to_ddl(schema, joins),
            extra_rules=rules,
        )

    async def _schema_linking(self, question: str) -> tuple[dict[str, Any], list, bool]:
        """Select one fact table; build schema via hybrid columns + optional values."""
        logger.info(f"load schema linking for question: {question}")
        table, explicit_granularity = await self._select_traffic_insight_table(question)
        schema = await asyncio.to_thread(self._schema_for_selected_table, table)
        if not schema:
            raise _scenario_error("Traffic Insight schema assembly returned empty schema", f"table={table}")
        bare = next(iter(schema))
        columns = schema[bare].get("columns") or {}
        with_values = sum(1 for info in columns.values() if str(info.get("example_values") or "").strip())
        logger.info(
            "Traffic Insight perceptor step=assemble_schema table={} columns={} with_values={} description={}",
            bare,
            len(columns),
            with_values,
            schema[bare].get("description") or "",
        )
        return schema, [], explicit_granularity

    async def _select_traffic_insight_table(self, question: str) -> tuple[str, bool]:
        need = await self._extract_need_columns(question)
        need_d: set[str] = need["need_d"]
        need_m: set[str] = need["need_m"]
        try:
            assert_need_fields(need_d, need_m)
        except ValueError as exc:
            raise _scenario_error("Traffic Insight field set is empty", str(exc)) from exc

        table_to_fields, recall_mode = await asyncio.to_thread(
            self._recall_tables_by_field_eq,
            need_d,
            need_m,
        )
        candidates = tables_from_field_index(table_to_fields)
        logger.info(
            "Traffic Insight perceptor step=field_eq_index mode={} tables={}",
            recall_mode,
            len(candidates),
        )
        if not candidates:
            raise _scenario_error(
                "Traffic Insight table recall returned no tables after field EQ select",
                f"need_d={sorted(need_d)}; need_m={sorted(need_m)}; mode={recall_mode}",
            )

        families = build_families_from_tables(candidates, exclude_prefixes=self._exclude_prefixes)
        logger.info(
            "Traffic Insight perceptor step=build_families count={}",
            len(families),
        )
        if not families:
            raise _scenario_error(
                "Traffic Insight table family catalog is empty",
                f"need_d={sorted(need_d)}; need_m={sorted(need_m)}; candidate_tables={len(candidates)}",
            )

        selected = select_families_by_max_need_hits(families, table_to_fields)
        logger.info(
            "Traffic Insight perceptor step=select_max_need_hit_families count={}",
            len(selected),
        )
        if not selected:
            raise _scenario_error(
                "Traffic Insight table family catalog is empty after max need-hit select",
                f"need_d={sorted(need_d)}; need_m={sorted(need_m)}; families={len(families)}",
            )

        rep_tables = [family["representative_table"] for family in selected]
        columns_map = await asyncio.to_thread(self._hybrid_columns_for_tables, rep_tables)
        logger.info(
            "Traffic Insight perceptor step=fetch_hybrid_cols rep_tables={}",
            len(rep_tables),
        )
        try:
            enriched = enrich_families_with_columns(selected, columns_map)
        except ValueError as exc:
            raise _scenario_error(
                "Traffic Insight hybrid columns incomplete for family enrich",
                str(exc),
            ) from exc
        if not enriched:
            raise _scenario_error(
                "Traffic Insight table family catalog is empty after column enrich",
                f"need_d={sorted(need_d)}; need_m={sorted(need_m)}",
            )

        narrowed = select_families_by_min_extra_fields_converged(
            enriched,
            need_d=need_d,
            need_m=need_m,
            limit=self._candidate_table_families_limit,
        )
        logger.info(
            "Traffic Insight perceptor step=select_min_extra_families count={}",
            len(narrowed),
        )
        if not narrowed:
            raise _scenario_error(
                "Traffic Insight table family catalog is empty after min-extra select",
                f"need_d={sorted(need_d)}; need_m={sorted(need_m)}; enriched={len(enriched)}",
            )

        selection = await self._select_traffic_insight_table_family(question, narrowed)
        table = resolve_family_selection(selection, narrowed)
        if not table:
            raise _scenario_error("Traffic Insight table family selection returned no valid table")
        logger.info(
            "Traffic Insight perceptor step=resolve_table selection={} table={}",
            selection,
            table,
        )
        return table, bool(selection and selection.get("explicit_granularity"))

    async def _extract_need_columns(self, question: str) -> dict[str, set[str]]:
        payload = await execute_with_llm_json(
            self._model,
            self._prompt_dir,
            "filter_traffic_insight_fields_",
            {"question": question},
        )
        logger.info("Traffic Insight perceptor step=llm_extract_field fields={}", payload)
        try:
            normalized = normalize_traffic_insight_fields(payload)
        except (TypeError, ValueError) as exc:
            raise _scenario_error("Traffic Insight field extraction returned invalid fields", str(exc)) from exc
        logger.info(
            "Traffic Insight perceptor step=classify_fields need_d={} need_m={}",
            sorted(normalized["need_d"]),
            sorted(normalized["need_m"]),
        )
        return normalized

    async def _select_traffic_insight_table_family(
        self,
        question: str,
        families: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        table_families_info = format_traffic_insight_table_family_prompt_context(families)
        parsed = await execute_with_llm_json(
            self._model,
            self._prompt_dir,
            "filter_traffic_insight_table_family_",
            {
                "question": question,
                "tables": table_families_info,
            },
        )
        logger.info("Traffic Insight perceptor step=llm_select_family result={}", parsed)
        if not isinstance(parsed, dict):
            return None
        family_name = str(parsed.get("family_name") or "").strip()
        granularity = str(parsed.get("granularity") or "").strip()
        if not family_name or not granularity:
            return None
        return {
            "family_name": family_name,
            "granularity": granularity,
            "explicit_granularity": parsed.get("explicit_granularity") is True,
        }

    def _recall_tables_by_field_eq(self, need_d: set[str], need_m: set[str]) -> tuple[dict[str, set[str]], str]:
        """Paginate EQ searches into ``table → {hit fields}``; no table-level rank truncation."""
        fields = sorted((need_d or set()) | (need_m or set()))
        table_to_fields: dict[str, set[str]] = {}
        any_field_hit = False
        for field in fields:
            hit_tables = self._stream_field_eq_into_index(field, table_to_fields)
            kind = "dimension" if field in need_d else "metric"
            logger.info(
                "Traffic Insight perceptor step=stat_field_hit_tables field={} kind={} hit_tables={}",
                field,
                kind,
                hit_tables,
            )
            if hit_tables == 0:
                logger.warning(
                    "field hits no tables, field={} kind={}",
                    field,
                    kind,
                )
            else:
                any_field_hit = True

        if fields and not any_field_hit:
            raise _scenario_error(
                "Traffic Insight all field EQ searches returned no tables",
                (
                    f"need_d={sorted(need_d)}; need_m={sorted(need_m)}; "
                    f"db_id={self._db or ''}; catalog/backend mismatch or wrong DATABASE.db_id likely"
                ),
            )

        return table_to_fields, "field_eq_index"

    def _stream_field_eq_into_index(self, field: str, table_to_fields: dict[str, set[str]]) -> int:
        """Page search/basic; fold each page into inverted index immediately (no per-field full set)."""
        page_size = max(1, self._column_eq_page_size)
        offset = 0
        max_offset = max(0, self._column_eq_max_offset)
        while offset <= max_offset:
            payload = {
                "typeName": "data_column",
                "limit": page_size,
                "offset": offset,
                "entityFilters": self._column_eq_entity_filters(field),
                "attributes": ["db_name_en", "table_name_en", "column_name_en"],
            }
            result = self._call_semantic_service(self.semantic_client.search_basic, payload)
            entities = result.get("entities") if isinstance(result, dict) else None
            page_count = len(entities) if isinstance(entities, list) else 0
            page_tables = extract_tables_from_column_search(result, database_name=self._db or None)
            # Drop raw page payload after extracting table ids; only keep index growth.
            del result, entities
            added = add_tables_to_field_index(
                table_to_fields,
                field=field,
                tables=page_tables,
                exclude_prefixes=self._exclude_prefixes,
            )
            logger.info(
                "Traffic Insight perceptor step=call_column_by_page field={} offset={} entities={} indexed={}",
                field,
                offset,
                page_count,
                added,
            )
            if page_count < page_size:
                break
            offset += page_size
        else:
            raise _scenario_error(
                "Traffic Insight column EQ pagination exceeded safety offset",
                f"field={field}; offset={offset}; page_size={page_size}; max_offset={max_offset}",
            )
        return sum(1 for fields in table_to_fields.values() if field in fields)

    def _column_eq_entity_filters(self, field: str) -> dict[str, Any]:
        """Build search/basic entityFilters; AND db_name_en when DATABASE.db_id is set."""
        column_filter = {
            "attributeName": "column_name_en",
            "operator": "EQ",
            "attributeValue": field,
        }
        db_name = str(self._db or "").strip()
        if not db_name:
            return column_filter
        return {
            "condition": "AND",
            "criterion": [
                column_filter,
                {
                    "attributeName": "db_name_en",
                    "operator": "EQ",
                    "attributeValue": db_name,
                },
            ],
        }

    def _hybrid_columns_for_tables(self, tables: list[str]) -> dict[str, dict[str, Any]]:
        """Batch hybrid/table-columns for every table; retry missing once; never drop silently."""
        merged: dict[str, dict[str, Any]] = {}
        qualified = [qualify_table_name(name, self._db or None) for name in tables]
        pending = list(dict.fromkeys(qualified))
        batch_size = max(1, self._hybrid_batch_size)
        max_passes = 2  # initial fetch + one retry for missing tables

        for pass_idx in range(max_passes):
            if not pending:
                break
            total_batches = (len(pending) + batch_size - 1) // batch_size
            for batch_count, start in enumerate(range(0, len(pending), batch_size), start=1):
                stop = start + batch_size
                chunk = pending[start:stop]
                raw = self._call_semantic_service(self.semantic_client.hybrid_table_columns, chunk)
                before = len(merged)
                merged.update(parse_hybrid_table_columns(raw))
                logger.info(
                    "Traffic Insight perceptor step=fetch_hybrid_cols:batch pass={} batch={}/{} keys_added={}",
                    pass_idx + 1,
                    batch_count,
                    total_batches,
                    len(merged) - before,
                )
            pending = tables_missing_from_hybrid_columns(qualified, merged)
            if pending:
                logger.warning(
                    "Traffic Insight hybrid columns missing after pass={} tables={}",
                    pass_idx + 1,
                    pending,
                )

        if pending:
            raise _scenario_error(
                "Traffic Insight hybrid/table-columns did not return all requested tables",
                f"missing={pending}",
            )
        return merged

    def _schema_for_selected_table(self, table_name: str) -> dict[str, Any]:
        """Build DDL schema from hybrid (table+columns); supplement example_values from columns-info only."""
        hybrid = self._hybrid_columns_for_tables([table_name])
        schema = schema_from_hybrid_columns(table_name, hybrid)
        if not schema:
            return {}
        try:
            # Use the same single-call contract as other perceptors: default limit, no offset loop.
            columns_info = self._get_table_columns_info(table_name)
        except DataAgentError as exc:
            logger.warning(
                "Traffic Insight perceptor step=supplement_values failed table={} detail={}",
                table_name,
                exc,
            )
            return schema
        if not isinstance(columns_info, dict):
            columns_info = {}
        enriched = enrich_schema_example_values_from_columns_info(schema, table_name, columns_info)
        logger.info(
            "Traffic Insight perceptor step=supplement_values table={} info_cols={}",
            table_name,
            len(columns_info),
        )
        return enriched

    def _call_semantic_service(self, func, *args, **kwargs):
        """Same contract as the base perceptor: DataAgentError passthrough, ValueError → tool error."""
        try:
            return func(*args, **kwargs)
        except DataAgentError:
            raise
        except ValueError as exc:
            raise DataAgentError(
                source="tool",
                component="nl2sql",
                fact=str(exc),
            ) from exc

    def _get_table_columns_info(self, table_name: str) -> dict:
        return self._call_semantic_service(
            self.semantic_client.get_table_columns_info,
            table_name,
            limit=DEFAULT_NL2SQL_SEMANTIC_TABLE_COLUMNS_LIMIT,
        )
