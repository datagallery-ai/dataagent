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
import json
from pathlib import Path
from typing import Any

import requests

from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient, SemanticServiceError
from dataagent.agents.nl2sql.errors import SemanticServiceCallError
from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import (
    format_udn_evidence,
    iter_semantic_column_payloads,
    json_parser,
    schema_to_ddl,
    select_semantic_columns,
)
from dataagent.agents.nl2sql.workflow.state import NL2SQLState
from dataagent.core.managers.prompt_manager import PromptTemplate
from dataagent.utils.constants import (
    DEFAULT_NL2SQL_SCHEMA_TOP_K,
    DEFAULT_NL2SQL_SEMANTIC_JOINABLE_TABLES_LIMIT,
    DEFAULT_NL2SQL_SEMANTIC_TABLE_COLUMNS_LIMIT,
    DEFAULT_NL2SQL_SEMANTIC_TABLE_LIST_LIMIT,
    NL2SQL_PROMPT_PREFIX,
)
from dataagent.utils.log import logger


class PerceptorNode(BaseNL2SQLNode):
    def __init__(self, **kwargs):
        super().__init__(name="perceptor", **kwargs)
        self._semantic_client: SemanticServiceClient | None = None
        self.top_k = kwargs.get("top_k", DEFAULT_NL2SQL_SCHEMA_TOP_K)
        self.user_schema = kwargs.pop("user_schema", None)
        self.user_evidence = kwargs.pop("user_evidence", None)
        self.user_sql_rules = kwargs.pop("user_sql_rules", None)
        self.user_few_shot_examples = kwargs.pop("user_few_shot_examples", None)
        # UDN specific configuration
        udn_cfg: dict = self._get_agent_config("SEMANTIC_LAYER.udn", {}) or {}
        table_cfg: dict = udn_cfg.get("table_selection", {})
        self.table_llm_topk = table_cfg.get("llm_topk", 4)
        self.table_vector_topk = table_cfg.get("vector_topk", 20)
        evidence_cfg: dict = udn_cfg.get("evidence_selection", {})
        self.evidence_mode = evidence_cfg.get("mode", "keywords")
        self.evidence_topk = evidence_cfg.get("topk", 5)

    @property
    def semantic_client(self) -> SemanticServiceClient:
        """Return the lazily initialized semantic-service client."""
        if self._semantic_client is None:
            try:
                self._semantic_client = SemanticServiceClient.from_config(self._config_manager)
            except (AttributeError, ValueError) as exc:
                raise SemanticServiceCallError(detail=str(exc)) from exc
        return self._semantic_client

    def build_evidence_str(self, keywords: list[str] | None) -> str:
        catalog = self._udn_column_metadata()
        if self.evidence_mode == "keywords":
            selected = self._semantic_udn_columns(keywords, self.evidence_topk, catalog)
            return format_udn_evidence(selected, semantic=True) if selected else ""
        return format_udn_evidence(catalog, semantic=False)

    def udn_schema_linking(self, question: str, keywords: list[str] | None):
        candidates = self._vector_table_candidates(keywords)
        tables = self._select_udn_tables(question, candidates)
        return self.full_schema(allow_tables=tables)

    def schema_linking(self, keywords: list[str] | None):
        """
        state["schema"] = {
          "tbl1": {
            "description": "...",
            "columns": {
              "col1": {"description": "...", "value_type": "..."},
              "col2": {...}
            }
          }
          "tbl2": {...}
        }
        state["joins"] = [("tbl1.col1", "tbl2.col1"), ...]
        """
        if not keywords:
            return {}, []
        dt_set, tc_set, j_set, dt_desc, schema = set(), set(), set(), {}, {}
        raw = self._call_semantic_service(self.semantic_client.semantic_search_columns, self.db, keywords, self.top_k)
        for payload in iter_semantic_column_payloads(raw):
            for entry in payload.get("column_name_search") or []:
                if not isinstance(entry, dict) or not entry:
                    continue
                d, t, c = next(iter(entry)).split(".")
                dt_set.add(f"{d}.{t}")
                tc_set.add(f"{t}.{c}")
        for item in self._get_table_list():
            ((dt, meta),) = item.items()
            dt_desc[dt] = meta.get("table_description", "")
        for dt in dt_set:
            _, t = dt.split(".")
            cols = self._get_table_columns_info(dt)
            schema[t] = {"description": dt_desc[dt], "columns": {}}
            for dtc, meta in cols.items():
                _, _, c = dtc.split(".")
                if f"{t}.{c}" not in tc_set:
                    continue
                schema[t]["columns"][c] = {
                    "description": meta.get("column_short_description", ""),
                    "value_type": meta.get("value_type", ""),
                    "example_values": meta.get("value_description", ""),
                }
        for j in self._get_joinable_tables(list(dt_set)):
            src, tgt = j["src"].split(".", 1)[1], j["target_column"][0].split(".", 1)[1]
            if src in tc_set and tgt in tc_set:
                j_set.add((src, tgt))
        return schema, sorted(j_set)

    def full_schema(self, allow_tables: list[str] | None = None):
        j_set, dt_desc, schema = set(), {}, {}
        allow_set = {str(t).strip() for t in (allow_tables or []) if str(t).strip()} or None
        allow_names = {t.split(".", 1)[1] if "." in t else t for t in allow_set} if allow_set else None
        for item in self._get_table_list():
            ((dt, meta),) = item.items()
            if allow_set and dt not in allow_set and dt.split(".", 1)[1] not in allow_names:
                continue
            dt_desc[dt] = meta.get("table_description", "")
        for dt in dt_desc:
            t = dt.split(".", 1)[1]
            cols = self._get_table_columns_info(dt)
            schema[t] = {"description": dt_desc[dt], "columns": {}}
            for dtc, meta in cols.items():
                c = dtc.split(".", 2)[2]
                schema[t]["columns"][c] = {
                    "description": meta.get("column_short_description", ""),
                    "value_type": meta.get("value_type", ""),
                }
        for j in self._get_joinable_tables(list(dt_desc.keys())):
            j_set.add((j["src"].split(".", 1)[1], j["target_column"][0].split(".", 1)[1]))
        return schema, sorted(j_set)

    def _call_semantic_service(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except SemanticServiceError as exc:
            raise SemanticServiceCallError(detail=_semantic_service_error_detail(exc)) from exc
        except requests.RequestException as exc:
            raise SemanticServiceCallError(detail=str(exc)) from exc
        except ValueError as exc:
            raise SemanticServiceCallError(detail=str(exc)) from exc

    def _process(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
        self._trajectory_recorder.record_node_start(
            node_name="perceptor",
            purpose=f"Build schema information for question: {state['question']}",
        )
        for attr, key in [
            ("schema_str", self.user_schema),
            ("evidence", self.user_evidence),
            ("sql_rules", self.user_sql_rules),
            ("few_shot_examples", self.user_few_shot_examples),
        ]:
            state[attr] = self._load_prompt(key)
        if not state["schema_str"]:
            if self.sql_service_engine == "udn":
                state["schema"], state["joins"] = self.udn_schema_linking(state["question"], state["keywords"])
            else:
                state["schema"], state["joins"] = self.full_schema()
            state["schema_str"] = schema_to_ddl(state["schema"], state["joins"])
        if not state["evidence"] and self.sql_service_engine == "udn":
            state["evidence"] = self.build_evidence_str(state["keywords"])
        message = f"=== Perceptor ===\n{state['schema_str']}"
        logger.info(message)
        state["stream_message"] = message
        return state

    def _udn_column_metadata(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for col_key, meta in self._get_table_columns_info("udn.derived_metrics").items():
            if isinstance(meta, dict):
                out[str(col_key)] = dict(meta)
        return out

    def _semantic_udn_columns(
        self, keywords: list[str] | None, top_k: int, catalog: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        raw = self._call_semantic_service(self.semantic_client.semantic_search_columns, self.db, keywords, top_k)
        return select_semantic_columns(raw, catalog)

    def _vector_table_candidates(self, keywords: list[str] | None) -> list[dict[str, Any]]:
        if not keywords:
            return []
        best: dict[str, dict[str, Any]] = {}
        raw = self._call_semantic_service(
            self.semantic_client.vector_search_table_desc, self.db, keywords, self.table_vector_topk
        )
        for item in raw:
            if not isinstance(item, dict) or not item:
                continue
            hits = next(iter(item.values()))
            if not isinstance(hits, list):
                continue
            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                name = str(hit.get("table_name") or "").strip()
                if not name:
                    continue
                score = _as_score(hit.get("score"))
                if name not in best or score > best[name]["score"]:
                    best[name] = {
                        "table_name": name,
                        "table_description": str(hit.get("table_description") or "").strip(),
                        "score": score,
                    }
        return sorted(best.values(), key=lambda x: x["score"], reverse=True)

    def _select_udn_tables(self, question: str, candidates: list[dict[str, Any]]) -> list[str]:
        tables = [{"table_name": c["table_name"], "table_description": c["table_description"]} for c in candidates]
        context = {"top_n": self.table_llm_topk, "question": question, "tables": json.dumps(tables, ensure_ascii=False)}
        return json.loads(json_parser(self.execute_with_llm(context, action="filter_udn_table_")))

    def _load_prompt(self, name: str | None) -> str:
        if not name:
            return ""
        workspace = self._get_agent_config("WORKSPACE.path")
        name = name if name.endswith(".md") else f"{name}.md"
        if workspace:
            prompt_path = Path(name) if Path(name).is_file() else Path(workspace) / name
            logger.info(f"nl2sql get prompt_path: {prompt_path}")
            return prompt_path.read_text(encoding="utf-8")
        return PromptTemplate.from_package_relative(f"{NL2SQL_PROMPT_PREFIX}/user/{name}").content

    def _get_table_list(self) -> list:
        return self._call_semantic_service(
            self.semantic_client.get_table_list, self.db, limit=DEFAULT_NL2SQL_SEMANTIC_TABLE_LIST_LIMIT
        )

    def _get_table_columns_info(self, table_name: str) -> dict:
        return self._call_semantic_service(
            self.semantic_client.get_table_columns_info,
            table_name,
            limit=DEFAULT_NL2SQL_SEMANTIC_TABLE_COLUMNS_LIMIT,
        )

    def _get_joinable_tables(self, table_names: list[str]) -> list:
        return self._call_semantic_service(
            self.semantic_client.get_joinable_tables,
            table_names,
            limit=DEFAULT_NL2SQL_SEMANTIC_JOINABLE_TABLES_LIMIT,
        )


def _semantic_service_error_detail(exc: SemanticServiceError) -> str:
    parts = [f"method={exc.method}", f"path={exc.path}", f"status_code={exc.status_code}"]
    if exc.error_code:
        parts.append(f"error_code={exc.error_code}")
    if exc.error_message:
        parts.append(f"error_message={exc.error_message}")
    return ", ".join(parts)


def _as_score(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
