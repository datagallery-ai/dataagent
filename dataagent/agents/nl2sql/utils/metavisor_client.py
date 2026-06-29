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
import logging
from typing import Any

import requests

from dataagent.agents.nl2sql.errors import MetaVisorServiceError, ValueMatchServiceError
from dataagent.utils.constants import DEFAULT_NL2SQL_METAVISOR_COLUMN_LIMIT, DEFAULT_NL2SQL_VALUEMATCH_TOP_K

logger = logging.getLogger(__name__)

_Params = dict[str, Any] | list[tuple[str, Any]]


class MetaVisorClient:
    def __init__(self, metavisor_url: str):
        self.base_url = f"{metavisor_url}/api/semantic/v1/advanced-search/"
        self.s = requests.Session()
        self.headers = {"Accept": "application/json"}

    def get_table_list(self, db: str) -> list:
        return self._get("table-list", params={"databaseName": db, "limit": 1000})

    def get_table_columns_info(self, table_name: str) -> dict:
        return self._get(
            "table-columns-info", params={"tableName": table_name, "limit": DEFAULT_NL2SQL_METAVISOR_COLUMN_LIMIT}
        )

    def get_joinable_tables(self, table_names: list[str]) -> list:
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
        return self._get("joinable-tables", params=params)

    def semantic_search_column(self, db: str, keywords: list[str], top_k: int) -> dict:
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
        )

    def vector_search_table_desc(self, db: str, keywords: list[str], top_k: int) -> dict:
        return self._get(
            "vector-search-table-desc",
            params={
                "databaseName": db,
                "keywords": keywords,
                "topK": int(top_k),
            },
        )

    def semantic_search_tables(self, db: str, keywords: list[str], top_k: int) -> dict:
        return self._get(
            "semantic-search-tables",
            params={
                "databaseName": db,
                "keywords": keywords,
                "top_k": top_k,
            },
        )

    def _get(self, path: str, params: _Params | None = None):
        url = f"{self.base_url}{path}"
        try:
            resp = self.s.get(url, headers=self.headers, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise MetaVisorServiceError(detail=str(exc)) from exc


class ValueMatchClient:
    def __init__(self, valuematch_url: str):
        self.base_url = f"http://{valuematch_url}/api/v1/"
        self.s = requests.Session()
        self.headers = {"Accept": "application/json"}

    def check_value_exist(self, db: str, val: str) -> dict:
        return self._get("bloom/check", params={"database": db, "value": val})

    def check_value_match(
        self, db: str, table: str, column: str, question: str, top_k: int = DEFAULT_NL2SQL_VALUEMATCH_TOP_K
    ) -> dict:
        return self._get(
            "lsh/match", params={"database": db, "table": table, "column": column, "query": question, "top_k": top_k}
        )

    def _get(self, path: str, params: dict[str, Any] | None = None):
        url = f"{self.base_url}{path}"
        try:
            resp = self.s.get(url, headers=self.headers, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise ValueMatchServiceError(detail=str(exc)) from exc
