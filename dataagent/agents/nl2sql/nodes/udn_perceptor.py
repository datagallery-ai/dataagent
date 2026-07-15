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
from typing import Any

from dataagent.agents.nl2sql.nodes.perceptor import PerceptorNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import (
    iter_semantic_column_payloads,
    json_parser,
    schema_to_ddl,
)
from dataagent.agents.nl2sql.workflow.state import NL2SQLState
from dataagent.utils.log import logger


class UDNPerceptorNode(PerceptorNode):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        udn_cfg: dict = self._get_agent_config("SEMANTIC_LAYER.udn")
        table_cfg: dict = udn_cfg.get("table_selection", {})
        self.table_llm_topk = table_cfg.get("llm_topk", 4)
        self.table_vector_topk = table_cfg.get("vector_topk", 20)
        evidence_cfg: dict = udn_cfg.get("evidence_selection", {})
        self.evidence_mode = evidence_cfg.get("mode", "keywords")
        self.evidence_topk = evidence_cfg.get("topk", 5)

    def udn_schema_linking(self, question: str, keywords: list[str] | None):
        candidates = self._vector_table_candidates(keywords)
        tables = self._select_udn_tables(question, candidates)
        return self.full_schema(allow_tables=tables)

    def build_evidence(self, keywords: list[str] | None) -> str:
        catalog = self._udn_column_metadata()
        if self.evidence_mode == "keywords":
            selected = self._semantic_udn_columns(keywords, self.evidence_topk, catalog)
            return self._format_udn_evidence(selected, semantic=True) if selected else ""
        return self._format_udn_evidence(catalog, semantic=False)

    def _process(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
        state["sql_rules"] = self._load_prompt(self.user_sql_rules)
        state["schema"], state["joins"] = self.udn_schema_linking(state["question"], state["keywords"])
        state["schema_str"] = schema_to_ddl(state["schema"], state["joins"])
        keywords = self._keyword_extraction(state["question"])
        state["evidence"] = self.build_evidence(keywords)
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
        return self._select_semantic_columns(raw, catalog)

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
                score = self._as_score(hit.get("score"))
                if name not in best or score > best[name]["score"]:
                    best[name] = {
                        "table_name": name,
                        "table_description": str(hit.get("table_description") or "").strip(),
                        "score": score,
                    }
        return sorted(best.values(), key=lambda x: x["score"], reverse=True)

    def _as_score(self, value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _select_semantic_columns(self, raw: Any, catalog: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        by_tail = {k.split(".")[-1]: k for k in catalog}
        selected: dict[str, dict[str, Any]] = {}
        for payload in iter_semantic_column_payloads(raw):
            for entry in payload.get("column_name_search") or []:
                sid = next(iter(entry.keys()))
                src_key = sid if sid in catalog else by_tail.get(sid.split(".")[-1])
                if src_key and sid not in selected:
                    selected[sid] = dict(catalog[src_key])
        return selected

    def _format_udn_evidence(self, columns: dict[str, dict[str, Any]], semantic: bool = False) -> str:
        if not columns:
            return ""
        lines = ["## Semantic top-k metrics\n" if semantic else "## Full metric catalog\n"]
        for col in columns:
            meta = columns[col]
            lines.append(f"### {col.split('.')[-1]}")
            lines.append(f"- qualified_id: {col}")
            text = meta.get("column_short_description")
            if text:
                lines.append(f"- description: {text}")
            profile = meta.get("column_value_profile")
            lines.append(f"- formula: {profile}" if profile is not None and str(profile).strip() else "- formula: ")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _select_udn_tables(self, question: str, candidates: list[dict[str, Any]]) -> list[str]:
        tables = [{"table_name": c["table_name"], "table_description": c["table_description"]} for c in candidates]
        context = {"top_n": self.table_llm_topk, "question": question, "tables": json.dumps(tables, ensure_ascii=False)}
        return json.loads(json_parser(self.execute_with_llm(context, action="filter_udn_table_")))
