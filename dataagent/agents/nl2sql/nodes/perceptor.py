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
    iter_semantic_column_payloads,
    json_parser,
    schema_to_ddl,
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

    @property
    def semantic_client(self) -> SemanticServiceClient:
        if self._semantic_client is None:
            try:
                self._semantic_client = SemanticServiceClient.from_config(self._config_manager)
            except (AttributeError, ValueError) as exc:
                raise SemanticServiceCallError(detail=str(exc)) from exc
        return self._semantic_client

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
                parts = next(iter(entry)).split(".")
                if len(parts) != 3:
                    logger.warning(f"Perceptor: malformed column_name_search entry (expected d.t.c): {entry}")
                    continue
                d, t, c = parts
                dt_set.add(f"{d}.{t}")
                tc_set.add(f"{t}.{c}")
        for item in self._get_table_list():
            if not isinstance(item, dict) or not item:
                continue
            dt, meta = next(iter(item.items()))
            dt_desc[dt] = (meta or {}).get("table_description", "")
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
            try:
                src = j["src"].split(".", 1)[1]
                tgt = j["target_column"][0].split(".", 1)[1]
            except (KeyError, IndexError, ValueError) as exc:
                logger.warning(f"Perceptor: malformed joinable_table entry {j}: {exc}")
                continue
            if src in tc_set and tgt in tc_set:
                j_set.add((src, tgt))
        return schema, sorted(j_set)

    def full_schema(self, allow_tables: list[str] | None = None):
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
            schema[t] = {"description": dt_desc[dt], "columns": {}}
            for dtc, meta in cols.items():
                c = dtc.split(".", 2)[2]
                schema[t]["columns"][c] = {
                    "description": meta.get("column_short_description", ""),
                    "value_type": meta.get("value_type", ""),
                    "example_values": meta.get("value_description", ""),
                }
        for j in self._get_joinable_tables(list(dt_desc.keys())):
            try:
                src = j["src"].split(".", 1)[1]
                tgt = j["target_column"][0].split(".", 1)[1]
            except (KeyError, IndexError, ValueError) as exc:
                logger.warning(f"Perceptor.full_schema: malformed joinable_table entry {j}: {exc}")
                continue
            j_set.add((src, tgt))
        return schema, sorted(j_set)

    def _call_semantic_service(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except SemanticServiceError as exc:
            raise SemanticServiceCallError(detail=self._semantic_service_error_detail(exc)) from exc
        except requests.RequestException as exc:
            raise SemanticServiceCallError(detail=str(exc)) from exc
        except ValueError as exc:
            raise SemanticServiceCallError(detail=str(exc)) from exc

    def _process(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
        for attr, key in [
            ("schema_str", self.user_schema),
            ("evidence", self.user_evidence),
            ("sql_rules", self.user_sql_rules),
            ("few_shot_examples", self.user_few_shot_examples),
        ]:
            state[attr] = self._load_prompt(key)
        if not state["schema_str"]:
            state["schema"], state["joins"] = self.full_schema()
            state["schema_str"] = schema_to_ddl(state["schema"], state["joins"])
        message = f"=== Perceptor ===\n{state['schema_str']}"
        logger.info(message)
        state["stream_message"] = message
        return state

    def _load_prompt(self, name: str | None) -> str:
        """Load a user-supplied prompt file by name.

        Resolution order:
        1. If ``name`` is an existing file path → read it directly.
        2. If ``WORKSPACE.path`` is configured → look for ``<workspace>/<name>``;
           if found, read it.
        3. Fall back to the package-bundled prompt at
           ``nl2sql/prompts/user/<name>`` (via :class:`PromptTemplate`).

        This fallback prevents ``FileNotFoundError`` when the referenced prompt
        file exists in the package but has not been copied to the workspace.
        """
        if not name:
            return ""
        name = name if name.endswith(".md") else f"{name}.md"
        prompt_path = Path(name) if Path(name).is_file() else None
        if prompt_path is not None:
            logger.info(f"nl2sql load prompt (absolute): {prompt_path}")
            return prompt_path.read_text(encoding="utf-8")
        workspace = self._get_agent_config("WORKSPACE.path")
        if workspace:
            ws_path = Path(workspace) / name
            if ws_path.is_file():
                logger.info(f"nl2sql load prompt (workspace): {ws_path}")
                return ws_path.read_text(encoding="utf-8")
        logger.info(f"nl2sql load prompt (package fallback): {name}")
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

    def _keyword_extraction(self, question: str) -> list[str]:
        context = {"question": question}
        res = json.loads(json_parser(self.execute_with_llm(context, action="keyword_extraction_")))
        return res["keywords"]

    def _semantic_service_error_detail(self, exc: SemanticServiceError) -> str:
        parts = [f"method={exc.method}", f"path={exc.path}", f"status_code={exc.status_code}"]
        if exc.error_code:
            parts.append(f"error_code={exc.error_code}")
        if exc.error_message:
            parts.append(f"error_message={exc.error_message}")
        return ", ".join(parts)
