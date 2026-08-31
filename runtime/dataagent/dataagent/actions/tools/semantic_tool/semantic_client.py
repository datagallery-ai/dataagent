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
"""Unified REST client for semantic-service metadata APIs."""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

from dataagent.actions.tools.semantic_tool.auth import get_semantic_layer_auth
from dataagent.common_utils.outbound_tls import httpx_verify
from dataagent.core.errors import DataAgentError
from dataagent.utils.constants import (
    DEFAULT_SEMANTIC_SERVICE_JOINABLE_TABLES_LIMIT,
    DEFAULT_SEMANTIC_SERVICE_TABLE_COLUMNS_LIMIT,
    DEFAULT_SEMANTIC_SERVICE_TABLE_LIST_LIMIT,
)


class SemanticServiceClient:
    """Thin client for ``/api/semantic/v1`` APIs used by semantic tools."""

    def __init__(
        self,
        base_url: str,
        *,
        auth: tuple[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        """Create a client for the configured semantic-service base URL."""
        self.base_url = normalize_semantic_base_url(base_url)
        self.timeout = timeout
        self.auth = auth
        self.client = httpx.Client(
            timeout=timeout,
            auth=auth,
            headers={"Accept": "application/json"},
            verify=httpx_verify("semantic_layer"),
        )

    @classmethod
    def from_config(cls, config_manager: Any) -> SemanticServiceClient:
        """Build a semantic-service client from SEMANTIC_LAYER."""
        raw_base_url = config_manager.get("SEMANTIC_LAYER.base_url")
        if not raw_base_url:
            raise DataAgentError(
                source="config",
                component="semantic-layer",
                fact="SEMANTIC_LAYER.base_url 未配置",
            )

        auth = get_semantic_layer_auth(config_manager)

        timeout = _as_float(
            config_manager.get("SEMANTIC_LAYER.timeout", 30.0),
            30.0,
        )
        return cls(str(raw_base_url), auth=auth, timeout=timeout)

    def get_table_list(self, database_name: str, *, limit: int = DEFAULT_SEMANTIC_SERVICE_TABLE_LIST_LIMIT) -> list:
        """Get tables under a semantic database."""
        return self.get("advanced-search/table-list", params={"databaseName": database_name, "limit": limit})

    def get_table_columns_info(
        self, table_name: str, *, limit: int = DEFAULT_SEMANTIC_SERVICE_TABLE_COLUMNS_LIMIT
    ) -> dict:
        """Get column metadata for a table."""
        return self.get("advanced-search/table-columns-info", params={"tableName": table_name, "limit": limit})

    def semantic_retrieve(self, query: str) -> dict:
        """Retrieve the semantic bundle relevant to one natural-language query."""
        payload: dict[str, Any] = {"query": query}
        return self.post("semantic/retrieve", json=payload, headers={"Content-Type": "application/json"})

    def semantic_search_columns(
        self,
        database_name: str,
        keywords: list[str],
        top_k: int,
    ) -> list:
        """Search columns by semantic keywords."""
        return self.get(
            "advanced-search/semantic-search-columns",
            params={
                "databaseName": database_name,
                "keywords": keywords,
                "topK": top_k,
                "searchColumns": "true",
                "searchValues": "false",
            },
        )

    def vector_search_table_desc(self, database_name: str, keywords: list[str], top_k: int) -> list:
        """Search table descriptions by vector similarity."""
        return self.get(
            "advanced-search/vector-search-table-desc",
            params={
                "databaseName": database_name,
                "keywords": keywords,
                "topK": int(top_k),
            },
        )

    def get_joinable_tables(
        self, table_names: list[str], *, limit: int = DEFAULT_SEMANTIC_SERVICE_JOINABLE_TABLES_LIMIT
    ) -> list:
        """Get joinable table relationships."""
        normalized: list[str] = []
        dropped = 0
        for table_name in table_names:
            name = str(table_name or "").strip()
            if name:
                normalized.append(name)
            else:
                dropped += 1
        if dropped:
            logger.warning("joinable-tables: skipped {} empty table name(s)", dropped)
        if not normalized:
            return []

        params: list[tuple[str, Any]] = [("dbTableNames", table_name) for table_name in normalized]
        params.append(("limit", limit))
        return self.get("advanced-search/joinable-tables", params=params)

    def search_fulltext(
        self,
        query: str,
        *,
        type_name: str | None = None,
        limit: int = 25,
        offset: int = 0,
        exclude_deleted: bool = False,
    ) -> dict:
        """Run semantic-service full-text search."""
        params: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "offset": offset,
            "excludeDeletedEntities": "true" if exclude_deleted else "false",
        }
        if type_name:
            params["typeName"] = type_name
        return self.get("search/fulltext", params=params)

    def search_basic(self, payload: dict[str, Any]) -> dict:
        """Run semantic-service basic search."""
        return self.post("search/basic", json=payload, headers={"Content-Type": "application/json"})

    def search_dsl(self, query: str) -> dict:
        """Run semantic-service DSL search."""
        return self.get("search/dsl", params={"query": query}, headers={"Content-Type": "application/json"})

    def get_entity_by_unique_attribute(self, type_name: str, attr_name: str, attr_value: str) -> dict:
        """Get an entity by a unique attribute value."""
        path = f"entity/uniqueAttribute/type/{quote(type_name, safe='')}"
        return self.get(path, params={f"attr:{attr_name}": attr_value})

    def get_entity_by_guid(self, guid: str) -> dict:
        """Get an entity by GUID."""
        return self.get(f"entity/guid/{quote(guid, safe='')}")

    def list_retrieval_tables(self) -> dict:
        """List semantic-layer retrieval tables."""
        return self.get("retrieval/tables")

    def get_retrieval_table_schema(self, table: str) -> dict:
        """Get schema for a semantic-layer retrieval table."""
        return self.get(f"retrieval/tables/{quote(table, safe='')}/schema")

    def hybrid_table_columns(self, tables: list[str]) -> list:
        """Batch-fetch physical columns for the given tables via hybrid/table-columns."""
        normalized = [str(name).strip() for name in tables if str(name or "").strip()]
        if not normalized:
            return []
        return self.post(
            "hybrid/table-columns",
            json={"tables": normalized},
            headers={"Content-Type": "application/json"},
        )

    def get(self, path: str, *, params: Any = None, headers: dict[str, str] | None = None) -> Any:
        """Send a GET request and return JSON response."""
        return self._request("GET", path, params=params, headers=headers)

    def post(
        self,
        path: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Send a POST request and return JSON response."""
        return self._request("POST", path, json=json, headers=headers)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Send a request with JSON completeness checks and audit logging."""
        url = self._url(path)
        started = time.perf_counter()
        try:
            if method.upper() == "GET":
                response = self.client.get(
                    url,
                    params=params,
                    headers=headers,
                )
            elif method.upper() == "POST":
                response = self.client.post(
                    url,
                    json=json,
                    headers=headers,
                )
            else:
                raise ValueError(f"unsupported semantic client method: {method}")
            response.raise_for_status()
            _assert_json_content_type(response)
            try:
                payload = response.json()
            except ValueError as err:
                raise DataAgentError(
                    source="tool",
                    component="semantic-service",
                    fact=f"语义服务响应不是合法 JSON：{method} {path}",
                ) from err
            _assert_minimal_json_shape(payload, path=path)
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "semantic.audit method={} path={} status={} elapsed_ms={:.1f} success=true",
                method,
                path,
                getattr(response, "status_code", "-"),
                elapsed_ms,
            )
            return payload
        except DataAgentError:
            raise
        except httpx.TimeoutException:
            logger.error(
                "semantic.audit method={} path={} success=false error=timeout",
                method,
                path,
            )
            raise
        except httpx.HTTPStatusError as err:
            _log_http_status_error(err, method=method, path=path)
            raise
        except httpx.RequestError:
            logger.error(
                "semantic.audit method={} path={} success=false error=request",
                method,
                path,
            )
            raise
        except ValueError as err:
            error = DataAgentError(
                source="tool",
                component="semantic-service",
                fact=f"语义服务响应无效：{method} {path}",
            )
            logger.error(
                "semantic.audit source={} method={} path={} success=false error=response_validation",
                error.source,
                method,
                path,
            )
            raise error from err

    def _url(self, path: str) -> str:
        """Build an absolute URL from a relative API path."""
        return f"{self.base_url}/{path.lstrip('/')}"


def normalize_semantic_base_url(raw_url: str) -> str:
    """Normalize semantic-service host or API URL to ``/api/semantic/v1``."""
    base = str(raw_url).strip().rstrip("/")
    if not base:
        raise ValueError("validation error: semantic service base_url must not be empty")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", base):
        base = f"http://{base}"

    lower = base.lower()
    if lower.endswith("/api/semantic/v1"):
        return base
    if lower.endswith("/api/semantic"):
        return f"{base}/v1"
    if lower.endswith("/api"):
        return f"{base}/semantic/v1"

    return f"{base}/api/semantic/v1"


def _assert_json_content_type(response: httpx.Response) -> None:
    """JSON completeness: reject non-JSON Content-Type when the header is present."""
    raw_headers = getattr(response, "headers", None) or {}
    content_type = str(raw_headers.get("Content-Type") or raw_headers.get("content-type") or "").lower()
    if not content_type:
        return
    if "json" not in content_type and "javascript" not in content_type:
        raise ValueError(f"internal semantic service content-type error: expected json, got {content_type}")


def _assert_minimal_json_shape(payload: Any, *, path: str) -> None:
    """JSON completeness: body must be object, array, or null (not a bare scalar)."""
    if payload is None or isinstance(payload, (dict, list)):
        return
    raise ValueError(f"internal semantic service schema error: path={path} expected object/array")


def _log_http_status_error(err: httpx.HTTPStatusError, *, method: str, path: str) -> None:
    """Audit an HTTP status error without wrapping it for retry classification."""
    response = err.response
    status_code = response.status_code if response is not None else None
    upstream_code: str | None = None
    upstream_message: str | None = None

    if response is not None:
        payload = _response_json(response)
        if isinstance(payload, dict):
            upstream_code = _optional_str(payload.get("errorCode") or payload.get("error_code"))
            upstream_message = _optional_str(
                payload.get("errorMessage") or payload.get("error_message") or payload.get("message")
            )
        if not upstream_message:
            text = getattr(response, "text", "") or ""
            upstream_message = _truncate(text.strip(), 200) if text else None

    logger.error(
        "semantic.audit upstream_code={} upstream_message={} method={} path={} status={} success=false",
        upstream_code,
        upstream_message,
        method,
        path,
        status_code,
    )


def _response_json(response: httpx.Response) -> Any:
    """Return response JSON when possible."""
    try:
        return response.json()
    except ValueError:
        return None


def _optional_str(value: Any) -> str | None:
    """Convert a value to a non-empty string."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to a maximum length."""
    return text if len(text) <= max_len else f"{text[:max_len]}..."


def _as_float(value: Any, default: float) -> float:
    """Convert a value to float with fallback."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
