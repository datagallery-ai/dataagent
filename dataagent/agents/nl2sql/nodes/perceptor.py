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

from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from dataagent.agents.nl2sql.utils.metavisor_client import MetaVisorClient
from dataagent.agents.nl2sql.utils.nl2sql_utils import (
    format_udn_evidence,
    iter_semantic_column_payloads,
    json_parser,
    schema_to_ddl,
    select_semantic_columns,
)
from dataagent.agents.nl2sql.workflow.state import NL2SQLState
from dataagent.core.managers.prompt_manager import PromptTemplate
from dataagent.utils.constants import DEFAULT_NL2SQL_SCHEMA_TOP_K, NL2SQL_PROMPT_PREFIX
from dataagent.utils.log import logger


class PerceptorNode(BaseNL2SQLNode):
    def __init__(self, **kwargs):
        super().__init__(name="perceptor", **kwargs)
        metavisor_url = self._get_agent_config("METAVISOR.metavisor_url")
        self.metavisor_client = MetaVisorClient(metavisor_url)
        self.top_k = kwargs.get("top_k", DEFAULT_NL2SQL_SCHEMA_TOP_K)
        self.user_schema = kwargs.pop("user_schema", None)
        self.user_evidence = kwargs.pop("user_evidence", None)
        self.user_sql_rules = kwargs.pop("user_sql_rules", None)
        self.user_few_shot_examples = kwargs.pop("user_few_shot_examples", None)
        # UDN specific configuration
        udn_cfg: dict = self._get_agent_config("METAVISOR.udn", {}) or {}
        table_cfg: dict = udn_cfg.get("table_selection", {})
        self.table_llm_topk = table_cfg.get("llm_topk", 4)
        self.table_vector_topk = table_cfg.get("vector_topk", 20)
        evidence_cfg: dict = udn_cfg.get("evidence_selection", {})
        self.evidence_mode = evidence_cfg.get("mode", "keywords")
        self.evidence_topk = evidence_cfg.get("topk", 5)

    def build_evidence_str(self, keywords: list[str]) -> str:
        catalog = self._udn_column_metadata()
        if self.evidence_mode == "keywords":
            selected = self._semantic_udn_columns(keywords, self.evidence_topk, catalog)
            return format_udn_evidence(selected, semantic=True) if selected else ""
        return format_udn_evidence(catalog, semantic=False)

    def udn_schema_linking(self, question: str, keywords: list[str]):
        candidates = self._vector_table_candidates(keywords)
        tables = self._select_udn_tables(question, candidates)
        return self.full_schema(allow_tables=tables)

    def schema_linking(self, keywords: list[str]):
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
        dt_set, tc_set, j_set, dt_desc, schema = set(), set(), set(), {}, {}
        raw = self.metavisor_client.semantic_search_column(self.db, keywords, self.top_k)
        for payload in iter_semantic_column_payloads(raw):
            for entry in payload.get("column_name_search") or []:
                if not isinstance(entry, dict) or not entry:
                    continue
                d, t, c = next(iter(entry)).split(".")
                dt_set.add(f"{d}.{t}")
                tc_set.add(f"{t}.{c}")
        if self.sql_service_engine == "udn":
            for item in self.metavisor_client.semantic_search_tables(self.db, keywords, self.top_k):
                payload = next(iter(item.values()))
                for entry in payload["table_name_search"]:
                    d, t = next(iter(entry)).split(".")
                    dt_set.add(f"{d}.{t}")
        else:
            for item in self.metavisor_client.get_table_list(self.db):
                ((dt, meta),) = item.items()
                dt_desc[dt] = meta.get("table_description", "")
        for dt in dt_set:
            _, t = dt.split(".")
            cols = self.metavisor_client.get_table_columns_info(dt)
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
        for j in self.metavisor_client.get_joinable_tables(list(dt_set)):
            src, tgt = j["src"].split(".", 1)[1], j["target_column"][0].split(".", 1)[1]
            if src in tc_set and tgt in tc_set:
                j_set.add((src, tgt))
        return schema, sorted(j_set)

    def full_schema(self, allow_tables: list[str] | None = None):
        j_set, dt_desc, schema = set(), {}, {}
        allow_set = {str(t).strip() for t in (allow_tables or []) if str(t).strip()} or None
        allow_names = {t.split(".", 1)[1] if "." in t else t for t in allow_set} if allow_set else None
        for item in self.metavisor_client.get_table_list(self.db):
            ((dt, meta),) = item.items()
            if allow_set and dt not in allow_set and dt.split(".", 1)[1] not in allow_names:
                continue
            dt_desc[dt] = meta.get("table_description", "")
        for dt in dt_desc:
            t = dt.split(".", 1)[1]
            cols = self.metavisor_client.get_table_columns_info(dt)
            schema[t] = {"description": dt_desc[dt], "columns": {}}
            for dtc, meta in cols.items():
                c = dtc.split(".", 2)[2]
                schema[t]["columns"][c] = {
                    "description": meta.get("column_short_description", ""),
                    "value_type": meta.get("value_type", ""),
                }
        for j in self.metavisor_client.get_joinable_tables(list(dt_desc.keys())):
            j_set.add((j["src"].split(".", 1)[1], j["target_column"][0].split(".", 1)[1]))
        return schema, sorted(j_set)

    def _process(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
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
        for col_key, meta in self.metavisor_client.get_table_columns_info("udn.derived_metrics").items():
            if isinstance(meta, dict):
                out[str(col_key)] = dict(meta)
        return out

    def _semantic_udn_columns(
        self, keywords: list[str], top_k: int, catalog: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        raw = self.metavisor_client.semantic_search_column(self.db, keywords, top_k)
        return select_semantic_columns(raw, catalog)

    def _vector_table_candidates(self, keywords: list[str]) -> list[dict[str, Any]]:
        best: dict[str, dict[str, Any]] = {}
        for item in self.metavisor_client.vector_search_table_desc(self.db, keywords, self.table_vector_topk):
            for hit in next(iter(item.values())):
                name = str(hit["table_name"]).strip()
                score = float(hit.get("score") or 0.0)
                if name and (name not in best or score > best[name]["score"]):
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

    def _load_prompt(self, name: str) -> str:
        if not name:
            return ""
        workspace = self._get_agent_config("WORKSPACE.path")
        if workspace:
            prompt_path = Path(name) if Path(name).is_file() else Path(workspace) / f"{name}.md"
            logger.info(f"nl2sql get prompt_path: {prompt_path}")
            return prompt_path.read_text(encoding="utf-8")
        return PromptTemplate.from_package_relative(f"{NL2SQL_PROMPT_PREFIX}/user/{name}").content
