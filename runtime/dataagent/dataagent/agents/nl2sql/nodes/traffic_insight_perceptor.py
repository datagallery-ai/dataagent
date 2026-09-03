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
"""Traffic Insight NL2SQL Perceptor: column EQ recall → hybrid rank → one table → columns-info DDL."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from dataagent.agents.nl2sql.nodes.perceptor import PerceptorNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import schema_to_ddl
from dataagent.agents.nl2sql.utils.traffic_insight_field_normalizer import (
    assert_need_fields,
    normalize_traffic_insight_fields,
)
from dataagent.agents.nl2sql.utils.traffic_insight_table_recall import (
    add_tables_to_field_index,
    apply_coverage_filter,
    build_families_from_tables,
    enrich_schema_example_values_from_columns_info,
    extract_tables_from_column_search,
    format_traffic_insight_table_family_prompt_context,
    parse_hybrid_table_columns,
    qualify_table_name,
    rank_and_truncate_families,
    rank_tables_from_field_index,
    resolve_family_selection,
    schema_from_hybrid_columns,
    tables_missing_from_hybrid_columns,
)
from dataagent.agents.nl2sql.workflow.state import NL2SQLState
from dataagent.core.errors import DataAgentError
from dataagent.utils.log import logger


class TrafficInsightPerceptorNode(PerceptorNode):
    """Perceptor for traffic insight: no table-list; EQ recall + hybrid rank; final columns-info."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        insight_cfg: dict = self._get_agent_config("SEMANTIC_LAYER.traffic_insight", {}) or {}
        recall_cfg: dict = insight_cfg.get("recall", {}) or {}
        self._column_eq_page_size = int(recall_cfg.get("column_eq_page_size", 200))
        self._column_eq_max_offset = int(recall_cfg.get("column_eq_max_offset", 100_000))
        self._max_candidate_tables = int(recall_cfg.get("max_candidate_tables", 200))
        # Prompt-size gate after enrich+rank only; never truncate families before hybrid columns.
        self._max_llm_table_families = int(recall_cfg.get("max_llm_table_families", 20))
        prefixes = recall_cfg.get("exclude_table_prefix", ["dim"])
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        self._exclude_prefixes = tuple(str(p) for p in (prefixes or ["dim"]))
        self._hybrid_batch_size = int(recall_cfg.get("hybrid_batch_size", 50))

    async def _aprocess(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
        state["sql_rules"] = await asyncio.to_thread(self._load_prompt, self.user_sql_rules)
        state["sql_rules"] += f"\n现在为{date.today().year}年{date.today().month}月{date.today().day}日。"
        schema, joins = await self._traffic_insight_schema_linking(state["question"])
        state["schema"] = schema
        state["joins"] = joins
        state["schema_str"] = schema_to_ddl(schema, joins)
        message = f"=== Perceptor ===\n{state['schema_str']}"
        logger.info(message)
        state["stream_message"] = message
        return state

    async def _traffic_insight_schema_linking(self, question: str) -> tuple[dict[str, Any], list]:
        """Select one fact table; build schema via hybrid columns + optional values."""
        table = await self._select_traffic_insight_table(question)
        schema = await asyncio.to_thread(self._schema_for_selected_table, table)
        if not schema:
            raise DataAgentError(
                source="config",
                component="nl2sql",
                fact=f"Traffic Insight schema assembly returned empty schema；table={table}",
            )
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
        return schema, []

    async def _select_traffic_insight_table(self, question: str) -> str:
        need = await self._extract_need_columns(question)
        need_d: set[str] = need["need_d"]
        need_m: set[str] = need["need_m"]
        try:
            assert_need_fields(need_d, need_m)
        except ValueError as exc:
            raise DataAgentError(
                source="config",
                component="nl2sql",
                fact=f"Traffic Insight field set is empty；{exc}",
            ) from exc

        candidates, recall_mode = await asyncio.to_thread(
            self._recall_tables_by_field_eq,
            need_d,
            need_m,
        )
        logger.info(
            "Traffic Insight perceptor step=rank_table_hits mode={} count={}",
            recall_mode,
            len(candidates),
        )
        if not candidates:
            raise DataAgentError(
                source="config",
                component="nl2sql",
                fact=(
                    "Traffic Insight table recall returned no tables after field EQ select；"
                    f"need_d={sorted(need_d)}; need_m={sorted(need_m)}; mode={recall_mode}"
                ),
            )

        families = build_families_from_tables(candidates, exclude_prefixes=self._exclude_prefixes)
        logger.info(
            "Traffic Insight perceptor step=build_families count={}",
            len(families),
        )
        if not families:
            raise DataAgentError(
                source="config",
                component="nl2sql",
                fact=(
                    "Traffic Insight table family catalog is empty；"
                    f"need_d={sorted(need_d)}; need_m={sorted(need_m)}; candidate_tables={len(candidates)}"
                ),
            )

        rep_tables = [family["representative_table"] for family in families]
        columns_map = await asyncio.to_thread(self._hybrid_columns_for_tables, rep_tables)
        logger.info(
            "Traffic Insight perceptor step=fetch_hybrid_cols rep_tables={}",
            len(rep_tables),
        )
        try:
            covered = apply_coverage_filter(families, columns_map, need_d=need_d, need_m=need_m)
        except ValueError as exc:
            raise DataAgentError(
                source="tool",
                component="nl2sql",
                fact=f"Traffic Insight hybrid columns incomplete for family enrich；{exc}",
            ) from exc
        ranked = rank_and_truncate_families(
            covered,
            need_d=need_d,
            need_m=need_m,
            max_llm_table_families=self._max_llm_table_families,
        )
        ranked_names = [family["family_name"] for family in ranked]
        logger.info(
            "Traffic Insight perceptor step=rank_coverage count={} families={}",
            len(ranked),
            ranked_names,
        )
        if not ranked:
            raise DataAgentError(
                source="config",
                component="nl2sql",
                fact=(
                    "Traffic Insight table family catalog is empty after column enrich；"
                    f"need_d={sorted(need_d)}; need_m={sorted(need_m)}"
                ),
            )

        selection = await self._select_traffic_insight_table_family(question, ranked)
        table = resolve_family_selection(selection, ranked)
        if not table:
            raise DataAgentError(
                source="config",
                component="nl2sql",
                fact="Traffic Insight table family selection returned no valid table",
            )
        logger.info(
            "Traffic Insight perceptor step=resolve_table selection={} table={}",
            selection,
            table,
        )
        return table

    async def _extract_need_columns(self, question: str) -> dict[str, set[str]]:
        payload = await self.execute_with_llm_json({"question": question}, action="filter_traffic_insight_fields_")
        logger.info("Traffic Insight perceptor step=llm_extract_field fields={}", payload)
        try:
            normalized = normalize_traffic_insight_fields(payload)
        except (TypeError, ValueError) as exc:
            raise DataAgentError(
                source="llm",
                component="nl2sql",
                fact=f"Traffic Insight field extraction returned invalid fields；{exc}",
            ) from exc
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
    ) -> dict[str, str] | None:
        parsed = await self.execute_with_llm_json(
            {
                "question": question,
                "tables": format_traffic_insight_table_family_prompt_context(families),
            },
            action="filter_traffic_insight_table_family_",
        )
        logger.info("Traffic Insight perceptor step=llm_select_family result={}", parsed)
        if not isinstance(parsed, dict):
            return None
        family_name = str(parsed.get("family_name") or "").strip()
        granularity = str(parsed.get("granularity") or "").strip()
        return {"family_name": family_name, "granularity": granularity} if family_name and granularity else None

    def _recall_tables_by_field_eq(self, need_d: set[str], need_m: set[str]) -> tuple[list[str], str]:
        """Algorithm B: paginate EQ → stream into table→fields index → hit-count rank."""
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
            raise DataAgentError(
                source="config",
                component="nl2sql",
                fact=(
                    "Traffic Insight all field EQ searches returned no tables；"
                    f"need_d={sorted(need_d)}; need_m={sorted(need_m)}; "
                    f"db_id={self.db or ''}; catalog/backend mismatch or wrong DATABASE.db_id likely"
                ),
            )

        return rank_tables_from_field_index(
            table_to_fields,
            need_field_count=len(fields),
            max_candidate_tables=self._max_candidate_tables,
        )

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
            page_tables = extract_tables_from_column_search(result, database_name=self.db or None)
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
            raise DataAgentError(
                source="constraint",
                component="nl2sql",
                fact=(
                    "Traffic Insight column EQ pagination exceeded safety offset；"
                    f"field={field}; offset={offset}; page_size={page_size}; max_offset={max_offset}"
                ),
            )
        return sum(1 for fields in table_to_fields.values() if field in fields)

    def _column_eq_entity_filters(self, field: str) -> dict[str, Any]:
        """Build search/basic entityFilters; AND db_name_en when DATABASE.db_id is set."""
        column_filter = {
            "attributeName": "column_name_en",
            "operator": "EQ",
            "attributeValue": field,
        }
        db_name = str(self.db or "").strip()
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
        qualified = [qualify_table_name(name, self.db or None) for name in tables]
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
            raise DataAgentError(
                source="tool",
                component="nl2sql",
                fact=f"Traffic Insight hybrid/table-columns did not return all requested tables；missing={pending}",
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
