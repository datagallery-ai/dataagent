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
from typing import Any, Optional
from urllib.parse import quote

import requests
from loguru import logger

from dataagent.actions.tools.semantic_tool.auth import get_semantic_layer_auth
from dataagent.utils.constants import (
    DEFAULT_SEMANTIC_SERVICE_JOINABLE_TABLES_LIMIT,
    DEFAULT_SEMANTIC_SERVICE_TABLE_COLUMNS_LIMIT,
    DEFAULT_SEMANTIC_SERVICE_TABLE_LIST_LIMIT,
)


class SemanticServiceError(requests.HTTPError):
    """HTTP error returned by semantic-service with parsed service error fields."""

    def __init__(
        self,
        *,
        method: str,
        path: str,
        status_code: int | None,
        error_code: str | None = None,
        error_message: str | None = None,
        response: requests.Response | None = None,
    ) -> None:
        """Create an error with HTTP and semantic-service error details."""
        self.method = method
        self.path = path
        self.status_code = status_code
        self.error_code = error_code
        self.error_message = error_message

        self.classification_hint = "internal semantic service error"

        super().__init__(f"{self.classification_hint}: Semantic service {method} failed", response=response)

    def __str__(self) -> str:
        """Return semantic-service error details suitable for diagnostic logs."""
        parts = [
            self.classification_hint,
            f"method={self.method}",
            f"path={self.path}",
            f"status_code={self.status_code}",
        ]
        if self.error_code:
            parts.append(f"error_code={self.error_code}")
        if self.error_message:
            parts.append(f"error_message={self.error_message}")

        response = self.response
        if response is not None:
            response_url = _optional_str(getattr(response, "url", None))
            response_reason = _optional_str(getattr(response, "reason", None))
            response_headers = _response_headers(response)
            response_body = _response_body(response)
            if response_url:
                parts.append(f"response_url={response_url!r}")
            if response_reason:
                parts.append(f"response_reason={response_reason!r}")
            if response_headers:
                parts.append(f"response_headers={response_headers!r}")
            parts.append(f"response_body={response_body!r}")

        return ", ".join(parts)


class SemanticServiceClient:
    """Thin client for ``/api/semantic/v1`` APIs used by semantic tools."""

    def __init__(
        self,
        base_url: str,
        *,
        auth: tuple[str, str] | None = None,
        timeout: float = 30.0,
        verify: bool = True,
    ) -> None:
        """Create a client for the configured semantic-service base URL."""
        self.base_url = normalize_semantic_base_url(base_url)
        self.timeout = timeout
        self.verify = verify
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if auth:
            self.session.auth = auth

    @classmethod
    def from_config(cls, config_manager: Any) -> SemanticServiceClient:
        """Build a semantic-service client from SEMANTIC_LAYER."""
        raw_base_url = config_manager.get("SEMANTIC_LAYER.base_url")
        if not raw_base_url:
            raise ValueError("validation error: SEMANTIC_LAYER.base_url must be configured")

        auth = get_semantic_layer_auth(config_manager)

        timeout = _as_float(
            config_manager.get("SEMANTIC_LAYER.timeout", 30.0),
            30.0,
        )
        verify = _as_bool(
            config_manager.get("SEMANTIC_LAYER.verify_ssl", True),
            True,
        )
        return cls(str(raw_base_url), auth=auth, timeout=timeout, verify=verify)

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
                response = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                    verify=self.verify,
                )
            elif method.upper() == "POST":
                response = self.session.post(
                    url,
                    json=json,
                    headers=headers,
                    timeout=self.timeout,
                    verify=self.verify,
                )
            else:
                raise ValueError(f"unsupported semantic client method: {method}")
            response.raise_for_status()
            _assert_json_content_type(response)
            try:
                payload = response.json()
            except ValueError as err:
                raise ValueError(f"internal semantic service JSON response error: method={method}") from err
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
        except requests.HTTPError as err:
            service_err = _build_service_error(err, method=method, path=path)
            logger.error(
                "semantic.audit method={} path={} status={} success=false error_code={}",
                method,
                path,
                getattr(err.response, "status_code", None),
                getattr(service_err, "error_code", None),
            )
            raise service_err from err
        except requests.Timeout as err:
            wrapped = requests.RequestException(f"internal semantic service request failed: method={method}")
            logger.error("semantic.audit method={} path={} success=false error=timeout", method, path)
            raise wrapped from err
        except requests.RequestException as err:
            wrapped = requests.RequestException(f"internal semantic service request failed: method={method}")
            logger.error("semantic.audit method={} path={} success=false error=request", method, path)
            raise wrapped from err
        except ValueError:
            # JSON completeness checks (not covered by requests.* handlers).
            logger.error("semantic.audit method={} path={} success=false error=response_validation", method, path)
            raise

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


def _assert_json_content_type(response: requests.Response) -> None:
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


def _build_service_error(err: requests.HTTPError, *, method: str, path: str) -> SemanticServiceError:
    """Convert an HTTP error into a parsed semantic-service error."""
    response = err.response
    status_code = response.status_code if response is not None else None
    error_code: str | None = None
    error_message: str | None = None

    if response is not None:
        payload = _response_json(response)
        if isinstance(payload, dict):
            error_code = _optional_str(payload.get("errorCode") or payload.get("error_code"))
            error_message = _optional_str(
                payload.get("errorMessage") or payload.get("error_message") or payload.get("message")
            )
        if not error_message:
            # Keep error_message short; do not dump full bodies into INFO logs.
            error_message = _truncate(response.text.strip(), 200) if response.text else None

    return SemanticServiceError(
        method=method,
        path=path,
        status_code=status_code,
        error_code=error_code,
        error_message=error_message,
        response=response,
    )


def _response_json(response: requests.Response) -> Any:
    """Return response JSON when possible."""
    try:
        return response.json()
    except ValueError:
        return None


def _response_headers(response: requests.Response) -> dict[str, str]:
    """Return response headers with credential-bearing values redacted."""
    raw_headers = getattr(response, "headers", None)
    if raw_headers is None:
        return {}

    sensitive_headers = {"authorization", "cookie", "proxy-authorization", "set-cookie", "x-api-key"}
    return {
        str(key): "***REDACTED***" if str(key).lower() in sensitive_headers else str(value)
        for key, value in raw_headers.items()
    }


def _response_body(response: requests.Response) -> Optional[str]:  # noqa: UP045
    """Return a bounded response body for diagnostics."""
    response_text = getattr(response, "text", None)
    if response_text is None:
        return None
    return _truncate(str(response_text), 4_000)


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


def _as_bool(value: Any, default: bool) -> bool:
    """Convert common config values to bool."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    return bool(value)
