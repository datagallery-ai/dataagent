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
import re
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations, product
from typing import Any

import httpx

from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient
from dataagent.agents.bird.constants import (
    BIRD_PROMPT_PREFIX,
    DEFAULT_BIRD_SEMANTIC_JOINABLE_TABLES_LIMIT,
    DEFAULT_BIRD_SEMANTIC_TABLE_COLUMNS_LIMIT,
    DEFAULT_BIRD_SEMANTIC_TABLE_LIST_LIMIT,
)
from dataagent.agents.bird.errors import LLMOutputParseError, SchemaNotFoundError, SemanticServiceCallError
from dataagent.agents.bird.nodes.base_bird_node import BaseBirdNode
from dataagent.agents.bird.utils.bird_utils import schema_to_ddl
from dataagent.agents.bird.workflow.state import BirdState
from dataagent.core.errors import DataAgentError
from dataagent.core.managers.prompt_manager import PromptTemplate
from dataagent.utils.log import logger

_TRACE_MAX_TABLES = 12
_TRACE_MAX_COLUMNS = 80
_TRACE_MAX_VALUES_PER_COLUMN = 5
_TRACE_MAX_WARNINGS = 20
_QUALIFIED_COLUMN = re.compile(r"(?:[A-Za-z_][\w]*\.){1,2}[A-Za-z_][\w ()%-]*")
_SEMANTIC_DEGRADABLE_ERRORS = (SemanticServiceCallError, DataAgentError, httpx.HTTPError)
_LLM_DEGRADABLE_ERRORS = (LLMOutputParseError, DataAgentError, httpx.HTTPError, KeyError, TypeError, ValueError)


class PerceptorNode(BaseBirdNode):
    def __init__(self, **kwargs):
        super().__init__(name="perceptor", **kwargs)
        self._semantic_client: SemanticServiceClient | None = None  # noqa: UP045
        self.column_top_k = kwargs.get("column_top_k", 5)
        self.value_description_top_k = kwargs.get("value_description_top_k", 3)
        self.value_vector_top_k = kwargs.get("value_vector_top_k", 3)
        self.value_search_workers = kwargs.get("value_search_workers", 4)
        self.value_search_max_calls = kwargs.get("value_search_max_calls", 80)
        self.sample_count = kwargs.get("sample_count", 3)
        self.column_score_threshold = kwargs.get("column_score_threshold", 0.0)
        self.value_score_threshold = kwargs.get("value_score_threshold", 0.0)
        self.value_vector_score_threshold = kwargs.get("value_vector_score_threshold", 0.0)
        self.value_distance_threshold = kwargs.get("value_distance_threshold", 0.05)
        self.trace_max_tables = kwargs.get("trace_max_tables", _TRACE_MAX_TABLES)
        self.trace_max_columns = kwargs.get("trace_max_columns", _TRACE_MAX_COLUMNS)
        self.trace_max_values_per_column = kwargs.get("trace_max_values_per_column", _TRACE_MAX_VALUES_PER_COLUMN)
        self.trace_max_warnings = kwargs.get("trace_max_warnings", _TRACE_MAX_WARNINGS)

    @property
    def semantic_client(self) -> SemanticServiceClient:
        """Lazily build the semantic-layer client from agent config."""
        if self._semantic_client is None:
            try:
                self._semantic_client = SemanticServiceClient.from_config(self._config_manager)
            except DataAgentError:
                raise
            except (AttributeError, ValueError) as exc:
                raise DataAgentError(
                    source="config",
                    component="bird",
                    fact="SEMANTIC_LAYER.base_url 未配置",
                ) from exc
        return self._semantic_client

    @staticmethod
    def _evidence_columns(evidence: str, schema: dict) -> set[str]:
        evidence_folded = evidence.casefold()
        selected = set()
        for table, table_meta in schema.items():
            for column in table_meta.get("columns", {}):
                qualified = f"{table}.{column}"
                patterns = (f"`{column}`", qualified, column)
                if any(
                    re.search(rf"(?<![\w]){re.escape(pattern.casefold())}(?![\w])", evidence_folded)
                    for pattern in patterns
                ):
                    selected.add(qualified)
        return selected

    @staticmethod
    def _schema_for_columns(full_schema: dict, columns: set[str]) -> dict:
        schema = {}
        for qualified in sorted(columns):
            if "." not in qualified:
                continue
            table, column = qualified.split(".", 1)
            if table not in full_schema or column not in full_schema[table].get("columns", {}):
                continue
            schema.setdefault(
                table,
                {"description": full_schema[table].get("description", ""), "columns": {}},
            )["columns"][column] = dict(full_schema[table]["columns"][column])
        return schema

    @staticmethod
    def _column_exists(schema: dict, qualified: str) -> bool:
        if "." not in qualified:
            return False
        table, column = qualified.split(".", 1)
        return table in schema and column in schema[table].get("columns", {})

    @staticmethod
    def _fallback_keywords(text: str) -> list[str]:
        return re.findall(r"[A-Za-z0-9_]+(?:\s+[A-Za-z0-9_]+)?", text)[:20]

    @staticmethod
    def _record_score(scores: dict[str, dict[str, float]], column: str, source: str, payload: dict) -> None:
        raw_score = next(
            (payload.get(key) for key in ("score", "similarity", "distance") if payload.get(key) is not None),
            None,
        )
        if isinstance(raw_score, int | float):
            scores.setdefault(column, {})[source] = float(raw_score)

    @classmethod
    def _extract_relation_pairs(cls, raw: Any) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        stack = [raw]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                expression = str(item.get("expression") or "")
                columns = []
                for match in _QUALIFIED_COLUMN.finditer(expression):
                    columns.append(".".join(match.group(0).strip().split(".")[-2:]))
                if len(columns) >= 2:
                    pairs.add((columns[0], columns[1]))
                src = item.get("src")
                targets = item.get("target_column") or []
                if src and targets:
                    left = ".".join(str(src).split(".")[-2:])
                    right = ".".join(str(targets[0]).split(".")[-2:])
                    pairs.add((left, right))
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        return pairs

    def full_schema(
        self, allow_tables: list[str] | None = None, *, include_key_metadata: bool = False
    ) -> tuple[dict, list[tuple[str, str]]]:
        """Retrieve the complete schema and joins, optionally limited to selected tables."""
        j_set, dt_desc, schema = set(), {}, {}
        allow_set = {str(t).strip() for t in (allow_tables or []) if str(t).strip()} or None
        allow_names = {t.split(".", 1)[1] if "." in t else t for t in allow_set} if allow_set else None
        for item in self._get_table_list():
            if not isinstance(item, dict) or not item:
                continue
            dt, meta = next(iter(item.items()))
            if allow_set and dt not in allow_set and dt.split(".", 1)[1] not in allow_names:
                continue
            dt_desc[dt] = (meta or {}).get("table_description", "")
        for dt in dt_desc:
            t = dt.split(".", 1)[1]
            cols = self._get_table_columns_info(dt)
            columns = {}
            schema[t] = {"description": dt_desc.get(dt, ""), "columns": columns}
            for dtc, meta in cols.items():
                c = dtc.split(".", 2)[2]
                column = {
                    "description": meta.get("column_short_description", ""),
                    "value_type": meta.get("value_type", ""),
                    "example_values": meta.get("value_description", ""),
                }
                if include_key_metadata:
                    column["is_primary_key"] = bool(meta.get("is_primary_key", False))
                    column["is_foreign_key"] = bool(meta.get("is_foreign_key", False))
                columns[c] = column
        for j in self._get_joinable_tables(list(dt_desc.keys())):
            try:
                src_value = j.get("src", "")
                target_columns = j.get("target_column", [])
                src = src_value.split(".", 1)[1]
                tgt = target_columns[0].split(".", 1)[1]
            except (AttributeError, IndexError, TypeError, ValueError) as exc:
                logger.warning(f"Perceptor.full_schema: malformed joinable_table entry {j}: {exc}")
                continue
            j_set.add((src, tgt))
        return schema, sorted(j_set)

    async def multi_stage_schema_linking(self, question: str, evidence: str) -> tuple[dict, list, dict]:
        """Run the BIRD step2b-step2i equivalent against semantic-service APIs."""
        trace: dict[str, Any] = {"version": 1, "stages": [], "matched_values": {}, "warnings": []}
        full_schema, catalog_joins = await asyncio.to_thread(self.full_schema, include_key_metadata=True)
        table_names = list(full_schema)
        search_text = question if not evidence.strip() else f"{question}\nEvidence: {evidence}"
        try:
            keywords = await self._keyword_extraction(search_text)
        except _LLM_DEGRADABLE_ERRORS as exc:
            keywords = self._fallback_keywords(search_text)
            self._trace_warning(trace, "keyword_extraction", str(exc), "lexical_fallback")
        keywords = list(dict.fromkeys(str(keyword).strip() for keyword in keywords if str(keyword).strip()))[:20]
        trace["stages"].append({"stage": "keywords", "keywords": keywords})

        selected: set[str] = set()
        matched_values: dict[str, list[str]] = {}
        sources: dict[str, set[str]] = {}
        scores: dict[str, dict[str, float]] = {}
        evidence_columns = self._evidence_columns(evidence, full_schema)
        selected.update(evidence_columns)
        for column in evidence_columns:
            sources.setdefault(column, set()).add("evidence_exact")
        retrievals = await asyncio.to_thread(self._multi_stage_retrieve, keywords, table_names, trace)
        for source, raw in retrievals:
            self._collect_retrieval_hits(raw, source, selected, matched_values, sources, scores)
        self._append_candidate_stage(trace, "semantic_retrieval", selected, sources, scores)

        candidate_schema = self._schema_for_columns(full_schema, selected) or full_schema
        direct = await self._llm_column_selection("multi_stage_direct_", question, evidence, candidate_schema, trace)
        selected.update(direct)
        self._append_candidate_stage(
            trace,
            "direct_linking",
            direct,
            {column: {"llm_direct"} for column in direct},
        )

        reversed_columns = await self._llm_column_selection(
            "multi_stage_reversed_",
            question,
            evidence,
            self._schema_for_columns(full_schema, selected) or full_schema,
            trace,
        )
        selected.update(reversed_columns)
        self._append_candidate_stage(
            trace,
            "sql_reversed_linking",
            reversed_columns,
            {column: {"sql_reversed"} for column in reversed_columns},
        )
        selected = {column for column in selected if self._column_exists(full_schema, column)}
        if not selected:
            selected = {
                f"{table}.{column}"
                for table, table_meta in full_schema.items()
                for column in table_meta.get("columns", {})
            }
            self._trace_warning(trace, "selection", "no columns selected", "full_schema_fallback")

        selected_tables = {column.split(".", 1)[0] for column in selected}
        key_columns = set()
        for table in selected_tables:
            for column, metadata in full_schema[table].get("columns", {}).items():
                if metadata.get("is_primary_key") or metadata.get("is_foreign_key"):
                    key_columns.add(f"{table}.{column}")
        selected.update(key_columns)
        self._append_candidate_stage(
            trace,
            "key_completion",
            key_columns,
            {column: {"primary_or_foreign_key"} for column in key_columns},
        )

        catalog_key_joins = {
            join
            for join in catalog_joins
            if join[0].split(".", 1)[0] in selected_tables and join[1].split(".", 1)[0] in selected_tables
        }
        for join in catalog_key_joins:
            selected.update(join)

        path_joins, bridge_columns = await asyncio.to_thread(
            self._join_closure,
            sorted({column.split(".", 1)[0] for column in selected}),
            trace,
        )
        joins = sorted(set(path_joins) | catalog_key_joins)
        selected.update(column for column in bridge_columns if self._column_exists(full_schema, column))
        trace["stages"].append(
            {
                "stage": "join_closure",
                "joins": [list(join) for join in joins[: self.trace_max_columns]],
                "bridge_columns": sorted(bridge_columns)[: self.trace_max_columns],
            }
        )

        schema = self._apply_sample_values(full_schema, selected, matched_values, trace)
        trace["final"] = {
            "tables": list(schema)[: self.trace_max_tables],
            "column_count": sum(len(meta.get("columns", {})) for meta in schema.values()),
        }
        return schema, joins, trace

    def _apply_sample_values(
        self,
        full_schema: dict,
        selected: set[str],
        matched_values: dict[str, list[str]],
        trace: dict,
    ) -> dict:
        qualified_ids = [f"{self.db}.{column}" for column in sorted(selected)]
        samples = self._safe_semantic_call(
            trace,
            "column_sample_values",
            self.semantic_client.get_columns_sample_values,
            qualified_ids,
            self.sample_count,
            default={},
        )
        for qualified, values in samples.items():
            short = qualified.split(".", 1)[1] if qualified.startswith(f"{self.db}.") else qualified
            matched_values.setdefault(short, []).extend(str(value) for value in values)

        schema = self._schema_for_columns(full_schema, selected)
        for table, table_meta in schema.items():
            for column, column_meta in table_meta.get("columns", {}).items():
                values = list(dict.fromkeys(matched_values.get(f"{table}.{column}", [])))[
                    : self.trace_max_values_per_column
                ]
                if values:
                    column_meta["example_values"] = ":|".join(values)
                    trace["matched_values"][f"{table}.{column}"] = values
        trace["matched_values"] = dict(list(trace["matched_values"].items())[: self.trace_max_columns])
        return schema

    def _multi_stage_retrieve(self, keywords: list[str], table_names: list[str], trace: dict) -> list[tuple[str, Any]]:
        retrievals = [
            (
                "column_name",
                self._safe_semantic_call(
                    trace,
                    "column_name_search",
                    self.semantic_client.semantic_search_columns,
                    self.db,
                    keywords,
                    self.column_top_k,
                    default=[],
                ),
            ),
            (
                "value_description",
                self._safe_semantic_call(
                    trace,
                    "value_description_search",
                    self.semantic_client.semantic_search_columns,
                    self.db,
                    keywords,
                    self.value_description_top_k,
                    search_values=True,
                    default=[],
                ),
            ),
        ]
        pairs = list(product(keywords, table_names))
        if len(pairs) > self.value_search_max_calls:
            self._trace_warning(
                trace,
                "value_vector_search",
                f"capped {len(pairs)} keyword/table calls at {self.value_search_max_calls}",
                "bounded_call_batch",
            )
            pairs = pairs[: self.value_search_max_calls]
        batch_size = max(1, self.value_search_workers)
        with ThreadPoolExecutor(max_workers=max(1, self.value_search_workers)) as pool:
            for start in range(0, len(pairs), batch_size):
                end = start + batch_size
                calls = [
                    (
                        keyword,
                        table,
                        pool.submit(
                            self.semantic_client.vector_search_column_value,
                            keyword,
                            self.db,
                            table,
                            self.value_vector_top_k,
                        ),
                    )
                    for keyword, table in pairs[start:end]
                ]
                for keyword, table, future in calls:
                    try:
                        retrievals.append(("value_vector", future.result()))
                    except _SEMANTIC_DEGRADABLE_ERRORS as exc:
                        self._trace_warning(
                            trace,
                            "value_vector_search",
                            f"{keyword}/{table}: {exc}",
                            "empty_result",
                        )
        return retrievals

    async def _llm_column_selection(
        self,
        action: str,
        question: str,
        evidence: str,
        schema: dict,
        trace: dict,
    ) -> set[str]:
        context = {
            "question": question,
            "evidence": evidence,
            "schema": schema_to_ddl(schema, include_primary_keys=True),
        }
        try:
            response = await self.execute_with_llm_json(context, action=action)
        except _LLM_DEGRADABLE_ERRORS as exc:
            self._trace_warning(trace, action.rstrip("_"), str(exc), "semantic_candidates_only")
            return set()
        selection = response.get("selection", response) if isinstance(response, dict) else {}
        result = set()
        if isinstance(selection, dict):
            for table, columns in selection.items():
                if isinstance(columns, list):
                    result.update(f"{table}.{column}" for column in columns)
        return result

    def _join_closure(self, tables: list[str], trace: dict) -> tuple[list[tuple[str, str]], set[str]]:
        joins: set[tuple[str, str]] = set()
        bridge_columns: set[str] = set()
        for left, right in combinations(tables, 2):
            raw = self._safe_semantic_call(
                trace,
                "join_path",
                self.semantic_client.get_table_relations_path,
                f"{self.db}.{left}",
                f"{self.db}.{right}",
                5,
                default=[],
            )
            for first, second in self._extract_relation_pairs(raw):
                joins.add((first, second))
                bridge_columns.update((first, second))
        return sorted(joins), bridge_columns

    def _collect_retrieval_hits(
        self,
        raw: Any,
        source: str,
        selected: set[str],
        matched_values: dict[str, list[str]],
        sources: dict[str, set[str]],
        scores: dict[str, dict[str, float]],
    ) -> None:
        stack = [(raw, source, None)]
        while stack:
            item, current_source, parent_column = stack.pop()
            if isinstance(item, dict):
                self._collect_direct_retrieval_hit(
                    item,
                    current_source,
                    selected,
                    matched_values,
                    sources,
                    scores,
                )
                for key, value in item.items():
                    nested_source = {
                        "column_name_search": "column_name",
                        "column_value_match": "value_description",
                    }.get(key, current_source)
                    parts = str(key).split(".")
                    if len(parts) >= 3:
                        column = ".".join(parts[-2:])
                        self._collect_wrapped_retrieval_hit(
                            value,
                            column,
                            nested_source,
                            selected,
                            matched_values,
                            sources,
                            scores,
                        )
                    stack.append((value, nested_source, column if len(parts) >= 3 else parent_column))
            elif isinstance(item, list):
                stack.extend((value, current_source, parent_column) for value in item)

    def _collect_direct_retrieval_hit(
        self,
        item: dict,
        source: str,
        selected: set[str],
        matched_values: dict[str, list[str]],
        sources: dict[str, set[str]],
        scores: dict[str, dict[str, float]],
    ) -> None:
        db_name = item.get("db_name")
        table_name = item.get("table_name")
        column_name = item.get("column_name")
        if not (db_name and table_name and column_name):
            return
        column = f"{table_name}.{column_name}"
        if not self._retrieval_hit_passes(item, source):
            return
        selected.add(column)
        sources.setdefault(column, set()).add(source)
        self._record_score(scores, column, source, item)
        if item.get("value") is not None:
            matched_values.setdefault(column, []).append(str(item["value"]))

    def _collect_wrapped_retrieval_hit(
        self,
        value: Any,
        column: str,
        source: str,
        selected: set[str],
        matched_values: dict[str, list[str]],
        sources: dict[str, set[str]],
        scores: dict[str, dict[str, float]],
    ) -> None:
        if not isinstance(value, dict):
            return
        passing_value_hits = [
            hit
            for hit in (value.get("values") or [])
            if isinstance(hit, dict) and self._retrieval_hit_passes(hit, source)
        ]
        wrapper_passes = self._retrieval_hit_passes(value, source)
        if source != "column_name" and value.get("values") is not None:
            wrapper_passes = bool(passing_value_hits)
        if not wrapper_passes:
            return
        selected.add(column)
        sources.setdefault(column, set()).add(source)
        self._record_score(scores, column, source, value)
        candidate = value.get("value") or value.get("column_value")
        if candidate is not None:
            matched_values.setdefault(column, []).append(str(candidate))
        for hit in passing_value_hits:
            if hit.get("value") is not None:
                matched_values.setdefault(column, []).append(str(hit["value"]))
            self._record_score(scores, column, source, hit)

    def _retrieval_hit_passes(self, payload: dict, source: str) -> bool:
        distance = payload.get("distance")
        if isinstance(distance, int | float):
            return float(distance) <= self.value_distance_threshold
        score = payload.get("score", payload.get("similarity"))
        if not isinstance(score, int | float):
            return True
        if source == "column_name":
            threshold = self.column_score_threshold
        elif source == "value_vector":
            threshold = self.value_vector_score_threshold
        else:
            threshold = self.value_score_threshold
        return float(score) >= threshold

    def _append_candidate_stage(
        self,
        trace: dict,
        stage: str,
        columns: set[str],
        sources: dict[str, set[str]],
        scores: dict[str, dict[str, float]] | None = None,
    ) -> None:
        trace["stages"].append(
            {
                "stage": stage,
                "candidates": [
                    {
                        "column": column,
                        "sources": sorted(sources.get(column, set())),
                        "scores": (scores or {}).get(column, {}),
                    }
                    for column in sorted(columns)[: self.trace_max_columns]
                ],
            }
        )

    def _trace_warning(self, trace: dict, stage: str, reason: str, fallback: str) -> None:
        warnings = trace.setdefault("warnings", [])
        warning = {"stage": stage, "reason": reason[:500], "fallback": fallback}
        if len(warnings) < self.trace_max_warnings and warning not in warnings:
            warnings.append(warning)
        logger.warning(f"Perceptor multi-stage warning: {warning}")

    def _safe_semantic_call(self, trace: dict, stage: str, func, *args, default, **kwargs):
        try:
            return self._call_semantic_service(func, *args, **kwargs)
        except (SemanticServiceCallError, DataAgentError) as exc:
            self._trace_warning(trace, stage, str(exc), "empty_result")
            return default

    def _call_semantic_service(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except DataAgentError:
            raise
        except httpx.HTTPError as exc:
            raise SemanticServiceCallError(detail=str(exc)) from exc
        except ValueError as exc:
            raise DataAgentError(
                source="tool",
                component="bird",
                fact=str(exc),
            ) from exc

    async def _aprocess(self, state: BirdState, runtime: Any = None) -> BirdState:
        _ = runtime
        state.setdefault("schema_linking_error", "")
        state["sql_rules"] = PromptTemplate.from_package_relative(f"{BIRD_PROMPT_PREFIX}/user/sql_rules_bird").content
        failure_trace = {"version": 1, "stages": [], "matched_values": {}, "warnings": []}
        try:
            schema, joins, trace = await self.multi_stage_schema_linking(
                state.get("question", "").strip(), state.get("evidence", "").strip()
            )
        except _SEMANTIC_DEGRADABLE_ERRORS as exc:
            self._trace_warning(failure_trace, "multi_stage_schema_linking", str(exc), "full_schema_fallback")
            try:
                schema, joins = await asyncio.to_thread(self.full_schema, include_key_metadata=True)
            except _SEMANTIC_DEGRADABLE_ERRORS as fallback_exc:
                self._trace_warning(failure_trace, "full_schema_fallback", str(fallback_exc), "failed")
                state["schema_linking_error"] = (
                    f"multi-stage linking failed: {str(exc)[:500]}; "
                    f"full-schema fallback failed: {str(fallback_exc)[:500]}"
                )
                schema, joins = {}, []
            trace = failure_trace
        state["schema_linking_trace"] = trace
        if not schema and not state.get("schema_linking_error"):
            raise SchemaNotFoundError(detail="multi-stage schema linking returned no schema")
        state["schema"] = schema
        state["schema_str"] = schema_to_ddl(schema, joins, include_primary_keys=True)
        message = f"=== Perceptor ===\n{state.get('schema_str', '')}"
        logger.info(message)
        state["stream_message"] = message
        return state

    def _get_table_list(self) -> list:
        return self._call_semantic_service(
            self.semantic_client.get_table_list, self.db, limit=DEFAULT_BIRD_SEMANTIC_TABLE_LIST_LIMIT
        )

    def _get_table_columns_info(self, table_name: str) -> dict:
        return self._call_semantic_service(
            self.semantic_client.get_table_columns_info,
            table_name,
            limit=DEFAULT_BIRD_SEMANTIC_TABLE_COLUMNS_LIMIT,
        )

    def _get_joinable_tables(self, table_names: list[str]) -> list:
        return self._call_semantic_service(
            self.semantic_client.get_joinable_tables,
            table_names,
            limit=DEFAULT_BIRD_SEMANTIC_JOINABLE_TABLES_LIMIT,
        )

    async def _keyword_extraction(self, question: str) -> list[str]:
        context = {"question": question}
        res = await self.execute_with_llm_json(context, action="keyword_extraction_")
        return res["keywords"]
