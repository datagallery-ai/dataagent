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
from typing import Any
from urllib.parse import quote

import requests
from loguru import logger


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

        parts = [f"Semantic service {method} failed", f"path={path}", f"status_code={status_code}"]
        if error_code:
            parts.append(f"error_code={error_code}")
        if error_message:
            parts.append(f"error_message={error_message}")
        super().__init__(", ".join(parts), response=response)


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
        """Build a semantic-service client from SEMANTIC_LAYER, falling back to METAVISOR."""
        raw_base_url = (
            config_manager.get("SEMANTIC_LAYER.base_url")
            or config_manager.get("SEMANTIC_LAYER.url")
            or config_manager.get("METAVISOR.semantic_url")
            or config_manager.get("METAVISOR.metavisor_url")
            or config_manager.get("METAVISOR.url")
        )
        if not raw_base_url:
            raise ValueError("SEMANTIC_LAYER.base_url or METAVISOR.metavisor_url must be configured")

        username = config_manager.get("SEMANTIC_LAYER.username") or config_manager.get("METAVISOR.username")
        password = config_manager.get("SEMANTIC_LAYER.password") or config_manager.get("METAVISOR.password")
        auth = _build_auth(username, password)

        timeout = _as_float(
            config_manager.get("SEMANTIC_LAYER.timeout", config_manager.get("METAVISOR.timeout", 30.0)),
            30.0,
        )
        verify = _as_bool(
            config_manager.get("SEMANTIC_LAYER.verify_ssl", config_manager.get("METAVISOR.verify_ssl", True)),
            True,
        )
        return cls(str(raw_base_url), auth=auth, timeout=timeout, verify=verify)

    def get_table_list(self, database_name: str) -> list:
        """Get tables under a semantic database."""
        return self.get("advanced-search/table-list", params={"databaseName": database_name})

    def get_table_columns_info(self, table_name: str, *, limit: int = 1000) -> dict:
        """Get column metadata for a table."""
        return self.get("advanced-search/table-columns-info", params={"tableName": table_name, "limit": limit})

    def semantic_search_tables(self, query: str, top_k: int) -> dict:
        """Search tables by query."""
        payload = {"query": query}
        return self.post("semantic/retrieve", json=payload, headers={"Content-Type": "application/json"})

    def semantic_search_columns(self, database_name: str, keywords: list[str], top_k: int) -> list:
        """Search columns by semantic keywords."""
        return self.get(
            "advanced-search/semantic-search-columns",
            params={
                "databaseName": database_name,
                "keywords": keywords,
                "topK": top_k,
                "searchColumns": "true",
                "searchValues": "false",
                "limit": 1000,
            },
        )

    def get_joinable_tables(self, table_names: list[str], *, limit: int = 2000) -> list:
        """Get joinable table relationships."""
        params: list[tuple[str, Any]] = [("dbTableNames", table_name) for table_name in table_names]
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
        url = self._url(path)
        try:
            response = self.session.get(
                url,
                params=params,
                headers=headers,
                timeout=self.timeout,
                verify=self.verify,
            )
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as err:
            service_err = _build_service_error(err, method="GET", path=path)
            logger.error(str(service_err))
            raise service_err from err
        except requests.RequestException as err:
            logger.error(f"Semantic service GET failed: path={path}, error={err}")
            raise

    def post(
        self,
        path: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Send a POST request and return JSON response."""
        url = self._url(path)
        try:
            response = self.session.post(
                url,
                json=json,
                headers=headers,
                timeout=self.timeout,
                verify=self.verify,
            )
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as err:
            service_err = _build_service_error(err, method="POST", path=path)
            logger.error(str(service_err))
            raise service_err from err
        except requests.RequestException as err:
            logger.error(f"Semantic service POST failed: path={path}, error={err}")
            raise

    def _url(self, path: str) -> str:
        """Build an absolute URL from a relative API path."""
        return f"{self.base_url}/{path.lstrip('/')}"


def normalize_semantic_base_url(raw_url: str) -> str:
    """Normalize host or legacy MetaVisor URL to ``/api/semantic/v1``."""
    base = str(raw_url).strip().rstrip("/")
    if not base:
        raise ValueError("semantic service base_url must not be empty")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", base):
        base = f"http://{base}"

    lower = base.lower()
    if lower.endswith("/api/semantic/v1"):
        return base
    if lower.endswith("/api/semantic"):
        return f"{base}/v1"
    if lower.endswith("/api"):
        return f"{base}/semantic/v1"

    for marker in ("/api/metavisor/v3", "/api/metavisor"):
        idx = lower.find(marker)
        if idx >= 0:
            return f"{base[:idx]}/api/semantic/v1"

    return f"{base}/api/semantic/v1"


def _build_auth(username: Any, password: Any) -> tuple[str, str] | None:
    """Build optional basic-auth credentials."""
    if not username and not password:
        return None
    if not username or not password:
        raise ValueError("SEMANTIC_LAYER.username/password or METAVISOR.username/password must be configured together")
    return str(username), str(password)


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
            error_message = _truncate(response.text.strip(), 500) if response.text else None

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
