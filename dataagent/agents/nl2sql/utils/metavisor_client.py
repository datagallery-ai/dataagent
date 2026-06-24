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
import logging
from typing import Any

import requests

from dataagent.agents.nl2sql.errors import MetaVisorServiceError, ValueMatchServiceError
from dataagent.agents.nl2sql.utils.trajectory_recorder import NL2SQLTrajectoryRecorder
from dataagent.utils.constants import DEFAULT_NL2SQL_METAVISOR_COLUMN_LIMIT, DEFAULT_NL2SQL_VALUEMATCH_TOP_K

logger = logging.getLogger(__name__)

_Params = dict[str, Any] | list[tuple[str, Any]]

_METAVISOR_RESULT_PREVIEW_MAX = 8000

_METAVISOR_TOOL_NAME_MAP = {
    "table-list": "metavisor_get_table_list",
    "table-columns-info": "metavisor_get_table_columns_info",
    "joinable-tables": "metavisor_get_joinable_tables",
    "semantic-search-columns": "metavisor_semantic_search_column",
    "vector-search-table-desc": "metavisor_vector_search_table_desc",
    "semantic-search-tables": "metavisor_semantic_search_tables",
}

_VALUEMATCH_TOOL_NAME_MAP = {
    "bloom/check": "valuematch_check_value_exist",
    "lsh/match": "valuematch_check_value_match",
}


def _serialize_params(params: _Params | None) -> dict[str, Any]:
    if params is None:
        return {}
    if isinstance(params, dict):
        serialized = {}
        for k, v in params.items():
            if isinstance(v, list):
                serialized[k] = [str(i) if not isinstance(i, str) else i for i in v]
            else:
                serialized[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
        return serialized
    serialized: dict[str, Any] = {}
    for k, v in params:
        serialized.setdefault(k, [])
        if isinstance(v, list):
            serialized[k].extend([str(i) if not isinstance(i, str) else i for i in v])
        else:
            serialized[k].append(v if isinstance(v, (str, int, float, bool)) else str(v))
    for k in list(serialized):
        if isinstance(serialized[k], list) and len(serialized[k]) == 1:
            serialized[k] = serialized[k][0]
    return serialized


class MetaVisorClient:
    def __init__(self, metavisor_url: str):
        self.base_url = f"{metavisor_url}/api/metaVisor/v3/advanced-search/"
        self.s = requests.Session()
        self.headers = {"Accept": "application/json"}
        self._trajectory_recorder: NL2SQLTrajectoryRecorder | None = None

    def set_trajectory_recorder(self, recorder: NL2SQLTrajectoryRecorder | None) -> None:
        self._trajectory_recorder = recorder

    def get_table_list(self, db: str) -> list:
        purpose = f"Retrieve table list from MetaVisor to discover available tables in database {db}"
        return self._get("table-list", params={"databaseName": db, "limit": 1000}, purpose=purpose)

    def get_table_columns_info(self, table_name: str) -> dict:
        purpose = f"Get column metadata for table {table_name} to understand its structure"
        return self._get(
            "table-columns-info", params={"tableName": table_name, "limit": DEFAULT_NL2SQL_METAVISOR_COLUMN_LIMIT}, purpose=purpose
        )

    def get_joinable_tables(self, table_names: list[str]) -> list:
        purpose = (
            f"Discover join relationships between tables {table_names} to determine how to connect them"
        )
        normalized: list[str] = []
        dropped = 0
        for t in table_names:
            name = (t or "").strip()
            if name:
                normalized.append(name)
            else:
                dropped += 1
        if dropped:
            logger.warning("joinable-tables: skipped %d empty table name(s)", dropped)
        if not normalized:
            return []
        params: list[tuple[str, Any]] = [("dbTableNames", t) for t in normalized]
        params.append(("limit", DEFAULT_NL2SQL_METAVISOR_COLUMN_LIMIT))
        return self._get("joinable-tables", params=params, purpose=purpose)

    def semantic_search_column(self, db: str, keywords: list[str], top_k: int) -> dict:
        kw_str = ", ".join(str(k) for k in keywords)
        purpose = (
            f"Search for columns semantically relevant to keywords: {kw_str} in database {db} (top_k={top_k})"
        )
        return self._get(
            "semantic-search-columns",
            params={
                "databaseName": db,
                "keywords": keywords,
                "topK": top_k,
                "searchColumns": "true",
                "searchValues": "false",
                "limit": DEFAULT_NL2SQL_METAVISOR_COLUMN_LIMIT,
            },
            purpose=purpose,
        )

    def vector_search_table_desc(self, db: str, keywords: list[str], top_k: int) -> dict:
        kw_str = ", ".join(str(k) for k in keywords)
        purpose = (
            f"Vector search for table descriptions matching keywords: {kw_str} in database {db} (top_k={top_k})"
        )
        return self._get(
            "vector-search-table-desc",
            params={
                "databaseName": db,
                "keywords": keywords,
                "topK": int(top_k),
            },
            purpose=purpose,
        )

    def semantic_search_tables(self, db: str, keywords: list[str], top_k: int) -> dict:
        kw_str = ", ".join(str(k) for k in keywords)
        purpose = (
            f"Search for tables semantically relevant to keywords: {kw_str} in database {db} (top_k={top_k})"
        )
        return self._get(
            "semantic-search-tables",
            params={
                "databaseName": db,
                "keywords": keywords,
                "top_k": top_k,
            },
            purpose=purpose,
        )

    def _get(self, path: str, params: _Params | None = None, purpose: str | None = None):
        url = f"{self.base_url}{path}"
        try:
            resp = self.s.get(url, headers=self.headers, params=params)
            resp.raise_for_status()
            result = resp.json()
            if self._trajectory_recorder is not None and purpose:
                tool_name = _METAVISOR_TOOL_NAME_MAP.get(path, f"metavisor_{path}")
                args = _serialize_params(params)
                tid = self._trajectory_recorder.record_tool_call(
                    tool_name=tool_name, args=args, purpose=purpose,
                )
                content = json.dumps(result, ensure_ascii=False)
                if len(content) > _METAVISOR_RESULT_PREVIEW_MAX:
                    content = content[:_METAVISOR_RESULT_PREVIEW_MAX] + "\n... (truncated)"
                self._trajectory_recorder.record_tool_result(content=content, tool_call_id=tid)
            if purpose:
                tool_name = _METAVISOR_TOOL_NAME_MAP.get(path, f"metavisor_{path}")
                result_preview = json.dumps(result, ensure_ascii=False)[:500]
                logging.debug(f"[MetaVisor] {tool_name} purpose={purpose} result_preview={result_preview}")
            return result
        except Exception as exc:
            if self._trajectory_recorder is not None and purpose:
                tool_name = _METAVISOR_TOOL_NAME_MAP.get(path, f"metavisor_{path}")
                args = _serialize_params(params)
                tid = self._trajectory_recorder.record_tool_call(
                    tool_name=tool_name, args=args, purpose=purpose,
                )
                self._trajectory_recorder.record_tool_result(
                    content=f"Error: {exc}", tool_call_id=tid,
                )
            raise MetaVisorServiceError(detail=str(exc)) from exc


class ValueMatchClient:
    def __init__(self, valuematch_url: str):
        self.base_url = f"http://{valuematch_url}/api/v1/"
        self.s = requests.Session()
        self.headers = {"Accept": "application/json"}
        self._trajectory_recorder: NL2SQLTrajectoryRecorder | None = None

    def set_trajectory_recorder(self, recorder: NL2SQLTrajectoryRecorder | None) -> None:
        self._trajectory_recorder = recorder

    def check_value_exist(self, db: str, val: str) -> dict:
        purpose = f"Check if value '{val}' exists in database {db} via bloom filter"
        return self._get("bloom/check", params={"database": db, "value": val}, purpose=purpose)

    def check_value_match(
        self, db: str, table: str, column: str, question: str, top_k: int = DEFAULT_NL2SQL_VALUEMATCH_TOP_K
    ) -> dict:
        purpose = f"Find similar values to '{question}' in {db}.{table}.{column} via LSH matching (top_k={top_k})"
        return self._get(
            "lsh/match",
            params={"database": db, "table": table, "column": column, "query": question, "top_k": top_k},
            purpose=purpose,
        )

    def _get(self, path: str, params: dict[str, Any] | None = None, purpose: str | None = None):
        url = f"{self.base_url}{path}"
        try:
            resp = self.s.get(url, headers=self.headers, params=params)
            resp.raise_for_status()
            result = resp.json()
            if self._trajectory_recorder is not None and purpose:
                tool_name = _VALUEMATCH_TOOL_NAME_MAP.get(path, f"valuematch_{path}")
                args = _serialize_params(params)
                tid = self._trajectory_recorder.record_tool_call(
                    tool_name=tool_name, args=args, purpose=purpose,
                )
                content = json.dumps(result, ensure_ascii=False)
                if len(content) > _METAVISOR_RESULT_PREVIEW_MAX:
                    content = content[:_METAVISOR_RESULT_PREVIEW_MAX] + "\n... (truncated)"
                self._trajectory_recorder.record_tool_result(content=content, tool_call_id=tid)
            return result
        except Exception as exc:
            if self._trajectory_recorder is not None and purpose:
                tool_name = _VALUEMATCH_TOOL_NAME_MAP.get(path, f"valuematch_{path}")
                args = _serialize_params(params)
                tid = self._trajectory_recorder.record_tool_call(
                    tool_name=tool_name, args=args, purpose=purpose,
                )
                self._trajectory_recorder.record_tool_result(
                    content=f"Error: {exc}", tool_call_id=tid,
                )
            raise ValueMatchServiceError(detail=str(exc)) from exc
