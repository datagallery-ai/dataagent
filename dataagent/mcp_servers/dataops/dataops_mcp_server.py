#!/usr/bin/env python3
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""DataOps adHoc MCP resource adapter.

Exposes the resource lifecycle tools expected by DataAgent and delegates SQL
validation to the DataOps OpenAPI endpoints ``/v3/adHoc/create`` and
``/v3/adHoc/query``. The server never connects to ClickHouse directly.

Authentication:
    Uses HMAC-SHA256 signature per DataOps OpenAPI spec:
    - openApiSign = HMAC-SHA256(appid + timestamp + random, secretKey)
    - Headers: openApiSign, timestamp, appid, random, serviceInstanceId
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load .env from the same directory as this script (if present)
_load_dir = Path(__file__).parent
load_dotenv(_load_dir / ".env", override=False)

LOGGER = logging.getLogger("dataops_mcp_server")
_SOURCE_TYPES = {"hive": 0, "clickhouse": 1}
_RUNNING_STATUSES = frozenset({666, 1, 3, 8})


class DataOpsClient:
    """Minimal client for the DataOps adHoc OpenAPI with HMAC-SHA256 auth."""

    def __init__(
        self,
        *,
        base_url: str,
        appid: str,
        secret_key: str,
        service_instance_id: str,
        source_type: str,
        resource_name: str,
        database_name: str,
        exec_user: str,
        engine: str,
        timeout_sec: int,
    ) -> None:
        normalized_source = str(source_type or "hive").strip().lower()
        if normalized_source not in _SOURCE_TYPES:
            raise ValueError("DATAOPS_SOURCE_TYPE must be hive or clickhouse")
        self._base_url = str(base_url or "").strip().rstrip("/")
        if not self._base_url:
            raise ValueError("DATAOPS_BASE_URL is required")
        self._appid = str(appid or "").strip()
        self._secret_key = str(secret_key or "").strip()
        self._service_instance_id = str(service_instance_id or "").strip()
        self._source_type = normalized_source
        self._resource_name = str(resource_name or "").strip()
        self._database_name = str(database_name or "").strip()
        self._exec_user = str(exec_user or "").strip()
        self._engine = str(engine or "").strip()
        self._timeout_sec = max(1, int(timeout_sec))

    @property
    def base_url(self) -> str:
        """Return the configured DataOps OpenAPI base URL."""
        return self._base_url

    @property
    def exec_user(self) -> str:
        """Execution user for temp table naming."""
        return self._exec_user

    @staticmethod
    def _byte_array_to_hex(b: bytes) -> str:
        """byte[] -> hex string（与官方 Java byteArrayToHexString 等价）。"""
        return "".join(f"{x:02x}" for x in b)

    @staticmethod
    def _hmac_sha256_hex(message: str, key_hex: str) -> str:
        """HmacSHA256(key hex-decode) -> 'security:' + uppercase hexdigest（与官方 Java 等价）。"""
        key_bytes = binascii.unhexlify(key_hex)
        return "security:" + hmac.new(key_bytes, message.encode(), hashlib.sha256).hexdigest().upper()

    def submit(self, sql: str) -> str:
        """Create one DataOps adHoc query and return its query id."""
        normalized_sql = str(sql or "").strip()
        if not normalized_sql:
            raise ValueError("envelope.command is required")
        body: dict[str, Any] = {
            "sourceType": _SOURCE_TYPES[self._source_type],
            "resourceName": self._resource_name,
            "databaseName": self._database_name,
            "execUser": self._exec_user,
            "querySql": normalized_sql,
        }
        if self._engine:
            body["engine"] = self._engine
        response = self._post_json("/v3/adHoc/create", body)
        # Detect error messages returned as plain strings (e.g., "Table 'xxx' already exists!")
        body_data = _response_body(response)
        if isinstance(body_data, str) and body_data.strip():
            stripped = body_data.strip()
            # If it looks like an error message rather than a query ID, raise
            if not stripped.isdigit() and "queryId" not in stripped.lower():
                raise ValueError(stripped)
        query_id = _extract_query_id(response)
        if not query_id:
            raise ValueError(f"DataOps adHoc/create returned no queryId: {response}")
        return query_id

    def query(self, query_id: str) -> dict[str, Any]:
        """Return the DataOps query result matching one query id."""
        normalized_id = str(query_id or "").strip()
        if not normalized_id:
            raise ValueError("job_id is required")
        response = self._post_json("/v3/adHoc/query", {"queryIdList": [normalized_id]})
        item = _find_query_result(response, normalized_id)
        if item is None:
            raise ValueError(f"DataOps adHoc/query returned no result for {normalized_id}, raw response: {response}")
        return item

    def get_job_log(self, query_id: str) -> dict[str, Any]:
        """Fetch the job execution log from OBS storage.

        The log URL and auth headers are embedded in the query result's
        ``logFileInfo`` field when the job fails.
        """
        normalized_id = str(query_id or "").strip()
        if not normalized_id:
            raise ValueError("job_id is required")

        result = self.query(normalized_id)
        log_file_info = result.get("logFileInfo") or {}

        url = log_file_info.get("url") or ""
        if not url:
            return {"error": "No logFileInfo URL in query result", "query_id": normalized_id}

        headers = log_file_info.get("headers") or {}
        if not headers:
            return {"error": "No auth headers in logFileInfo", "query_id": normalized_id}

        return self._fetch_log_from_obs(url, headers)

    def _fetch_log_from_obs(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        """Download log content from OBS using the provided pre-signed URL and headers."""
        try:
            response = httpx.get(
                url,
                headers=headers,
                timeout=self._timeout_sec,
            )
            if response.status_code >= 400:
                return {"error": f"OBS returned HTTP {response.status_code}: {response.text[:500]}"}
            return {"log_content": response.text, "status_code": response.status_code}
        except httpx.HTTPError as exc:
            return {"error": f"Failed to fetch log from OBS: {exc}"}

    def _build_auth_headers(self) -> dict[str, str]:
        """
        DataOps OpenAPI 鉴权（与官方 Java SDK 等价）：

        1. random = byteArrayToHexString(randomBytes[16])
        2. signString = appId + timestamp + random
        3. openApiSign = 'security:' + HMAC-SHA256(hex-decoded-secret, signString)
        """
        ts = str(int(time.time() * 1000))
        nonce = self._byte_array_to_hex(secrets.token_bytes(16))
        sign_string = f"{self._appid}{ts}{nonce}"
        return {
            "Content-Type": "application/json; charset=utf-8",
            "openApiSign": self._hmac_sha256_hex(sign_string, self._secret_key),
            "timestamp": ts,
            "appid": self._appid,
            "random": nonce,
            "serviceInstanceId": self._service_instance_id,
        }

    def _post_json(self, path: str, body: dict[str, Any]) -> Any:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            **self._build_auth_headers(),
        }
        url = f"{self._base_url}{path}"
        body_str = json.dumps(body, ensure_ascii=False)
        body_bytes = body_str.encode("utf-8")
        # Reconstruct a reproducible curl command for debugging
        curl_parts = ["curl", "-X", "POST", url]
        for k, v in headers.items():
            curl_parts.extend(["-H", f"{k}: {v}"])
        curl_parts.extend(["-d", json.dumps(body, ensure_ascii=False, indent=2)])
        LOGGER.info("DataOps curl: {}".format(" ".join(curl_parts)))

        # httpx forwards header names verbatim (no title-casing), unlike
        # urllib / http.client whose email.Message layer mangles keys like
        # "openApiSign" -> "Openapisign", breaking case-sensitive upstream
        # auth lookup.
        try:
            response = httpx.post(
                url,
                content=body_bytes,
                headers=headers,
                timeout=self._timeout_sec,
            )
            payload = response.text
        except httpx.HTTPError as exc:
            raise ValueError(f"DataOps unreachable at {self._base_url}: {exc}") from exc
        if response.status_code >= 400:
            detail = payload.strip()
            raise ValueError(detail or f"DataOps HTTP error: {response.status_code}")
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"DataOps returned non-JSON payload: {payload[:500]}") from exc


def _response_body(payload: Any) -> Any:
    """Unwrap common DataOps response envelopes."""
    if not isinstance(payload, dict):
        return payload
    for key in ("body", "data"):
        if key in payload:
            return payload[key]
    return payload


def _extract_query_id(payload: Any) -> str:
    """Extract queryId from a DataOps adHoc/create response."""
    body = _response_body(payload)
    if isinstance(body, dict):
        value = body.get("queryId")
        if value is not None and str(value).strip():
            return str(value).strip()
    if isinstance(body, (int, float)):
        return str(body)
    if isinstance(body, str) and body.strip():
        return body.strip()
    return ""


def _find_query_result(payload: Any, query_id: str) -> dict[str, Any] | None:
    """Find one query result in a DataOps adHoc/query response."""
    body = _response_body(payload)
    candidates = body if isinstance(body, list) else [body]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("queryId") or "").strip()
        if item_id == query_id or (not item_id and len(candidates) == 1):
            return item
    return None


def _status_payload(job_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Map a DataOps queryStatus to the resource MCP lifecycle contract."""
    try:
        raw_status = result.get("queryStatus")
        query_status = int(raw_status) if raw_status is not None else 0
    except (TypeError, ValueError):
        return {
            "status": "failed",
            "job_id": job_id,
            "error": f"DataOps returned invalid queryStatus: {result.get('queryStatus')}",
            "exit_code": 1,
        }
    if query_status == 5:
        return {"status": "completed", "job_id": job_id, "exit_code": 0}
    if query_status == 6:
        error = str(result.get("errorMsg") or result.get("errorDesc") or "DataOps SQL validation failed")
        log_file_info = result.get("logFileInfo") or {}
        payload: dict[str, Any] = {
            "status": "failed",
            "job_id": job_id,
            "error": error,
            "raw_result": result,
            "exit_code": 1,
        }
        if log_file_info:
            payload["logFileInfo"] = log_file_info
        return payload
    if query_status in {7, 10}:
        return {
            "status": "cancelled",
            "job_id": job_id,
            "error": str(result.get("errorMsg") or "DataOps task was cancelled"),
            "exit_code": 130,
        }
    if query_status in _RUNNING_STATUSES:
        return {"status": "running", "job_id": job_id, "summary": f"DataOps queryStatus={query_status}"}

    # Default: unknown status
    return {
        "status": "failed",
        "job_id": job_id,
        "error": f"DataOps returned unknown queryStatus: {query_status}",
        "exit_code": 1,
    }


def create_server(*, host: str, port: int, client: DataOpsClient) -> FastMCP:
    """Build the DataOps MCP server."""
    server = FastMCP(
        "DataOps AdHoc SQL Validator",
        instructions="Validate SQL through the DataOps adHoc OpenAPI.",
        host=host,
        port=port,
        streamable_http_path="/mcp",
        stateless_http=True,
    )

    @server.tool()
    def submit_job(envelope: dict[str, Any], allocation: dict[str, Any]) -> dict[str, Any]:
        """Submit SQL to DataOps via /v3/adHoc/create (all SQL types).

        CREATE TABLE statements also go through adHoc/create now — we no
        longer route them to /v3/dataAssets/create.
        """
        del allocation
        sql = str(envelope.get("command") or "").strip()
        if not sql:
            return {"status": "failed", "error": "envelope.command is empty", "exit_code": 1}

        try:
            job_id = client.submit(sql)
        except Exception as exc:
            LOGGER.warning(f"submit_job failed sql_excerpt={sql[:200]!r} error={exc}")
            return {"status": "failed", "error": str(exc), "exit_code": 1}
        LOGGER.info(f"submit_job job_id={job_id}")
        return {"status": "running", "job_id": job_id}

    @server.tool()
    def poll_job(job_id: str) -> dict[str, Any]:
        """Query the current DataOps adHoc status once."""
        try:
            raw_result = client.query(job_id)
            payload = _status_payload(job_id, raw_result)
        except ValueError as exc:
            payload = {"status": "failed", "job_id": job_id, "error": str(exc), "exit_code": 1}
        LOGGER.info("poll_job job_id={} status={}".format(job_id, payload.get("status")))
        return payload

    @server.tool()
    def collect_job(job_id: str) -> dict[str, Any]:
        """Collect terminal validation details from DataOps adHoc/query."""
        try:
            result = client.query(job_id)
            payload = _status_payload(job_id, result)
        except ValueError as exc:
            return {"status": "failed", "job_id": job_id, "error": str(exc), "exit_code": 1}
        if payload["status"] == "completed":
            payload["summary"] = "SQL validated successfully by DataOps"
        return payload

    @server.tool()
    def cancel_job(job_id: str) -> dict[str, Any]:
        """Report unsupported cancellation without calling an undocumented API."""
        return {
            "status": "error",
            "job_id": job_id,
            "error": "DataOps adHoc cancellation API is not configured",
            "exit_code": 1,
        }

    @server.tool()
    def get_job_log(job_id: str) -> dict[str, Any]:
        """Fetch detailed execution log from OBS storage for a failed job.

        When a job fails, the query result contains a ``logFileInfo`` field with
        a pre-signed OBS URL. This tool downloads and returns the log content.
        """
        try:
            log_result = client.get_job_log(job_id)
            if "error" in log_result:
                return {"status": "error", "job_id": job_id, "error": log_result["error"]}
            return {
                "status": "success",
                "job_id": job_id,
                "log_content": log_result.get("log_content", ""),
            }
        except ValueError as exc:
            return {"status": "error", "job_id": job_id, "error": str(exc), "exit_code": 1}
        except Exception as exc:
            return {"status": "error", "job_id": job_id, "error": f"get_job_log failed: {exc}", "exit_code": 1}

    return server


def _build_client() -> DataOpsClient:
    source_type = os.environ.get("DATAOPS_SOURCE_TYPE", "hive")
    if source_type.strip().lower() == "clickhouse":
        resource_name = os.environ.get("DATAOPS_CLICKHOUSE_RESOURCE_NAME", "")
    else:
        resource_name = os.environ.get("DATAOPS_HIVE_RESOURCE_NAME", "")
    return DataOpsClient(
        base_url=os.environ.get("DATAOPS_BASE_URL", ""),
        appid=os.environ.get("DATAOPS_APPID", ""),
        secret_key=os.environ.get("DATAOPS_SECRET_KEY", ""),
        service_instance_id=os.environ.get("DATAOPS_SERVICE_INSTANCE_ID", ""),
        source_type=source_type,
        resource_name=resource_name,
        database_name=os.environ.get("DATAOPS_DATABASE_NAME", ""),
        exec_user=os.environ.get("DATAOPS_EXEC_USER", ""),
        engine=os.environ.get("DATAOPS_ENGINE", ""),
        timeout_sec=int(os.environ.get("DATAOPS_REQUEST_TIMEOUT_SEC", "30")),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the DataOps adHoc MCP resource adapter.")
    parser.add_argument("--host", default=os.environ.get("DATAOPS_MCP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DATAOPS_MCP_PORT", "8767")))
    return parser.parse_args()


# Module-level server instance, required for `mcp dev` to discover the server object
mcp: FastMCP | None = None


def main() -> None:
    """Start the DataOps MCP resource adapter."""
    global mcp
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = _parse_args()
    client = _build_client()
    mcp = create_server(host=args.host, port=args.port, client=client)
    LOGGER.info(f"Starting DataOps MCP server at http://{args.host}:{args.port}/mcp")
    LOGGER.info(f"DataOps OpenAPI endpoint: {client.base_url}")
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
