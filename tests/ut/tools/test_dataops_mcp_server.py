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
"""Unit tests for :mod:`dataagent.mcp_servers.dataops.dataops_mcp_server`.

The MCP server is a thin adapter:

* ``DataOpsClient`` builds HMAC-SHA256 auth headers and POSTs to ``/v3/adHoc/...``
* ``_status_payload`` maps the cryptic ``queryStatus`` integer to the
  resource lifecycle vocabulary (``completed``/``failed``/``cancelled``/``running``)
* Module-level ``create_server`` registers ``submit_job``/``poll_job``/``collect_job``/
  ``cancel_job``/``get_job_log`` as FastMCP tools

Tests cover each layer end-to-end with ``httpx`` mocked and the real
``FastMCP`` tool picker exercising the underlying business logic.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dataagent.mcp_servers.dataops import dataops_mcp_server as srv

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(**overrides: Any) -> srv.DataOpsClient:
    """Build a ``DataOpsClient`` with sensible defaults so each test focuses
    on the field it actually cares about."""
    spec = {
        "base_url": "https://dataops.example.com/openapi",
        "appid": "appid-test",
        "secret_key": "abcd" * 16,  # 64 hex chars → binascii.unhexlify will accept
        "service_instance_id": "inst-1",
        "source_type": "hive",
        "resource_name": "hive_cluster_a",
        "database_name": "default",
        "exec_user": "alice",
        "engine": "",
        "timeout_sec": 30,
    }
    spec.update(overrides)
    return srv.DataOpsClient(**spec)


def _mock_post_response(payload: Any, status_code: int = 200) -> MagicMock:
    """Build a fake ``httpx`` response carrying a JSON body."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = json.dumps(payload)
    return resp


# ===========================================================================
# Pure utility helpers
# ===========================================================================


class TestByteArrayToHex:
    """byte[] -> hex string (matches the Java SDK byteArrayToHexString)."""

    def test_empty_bytes(self):
        assert srv.DataOpsClient._byte_array_to_hex(b"") == ""

    def test_prints_lowercase_hex(self):
        # 0xDE 0xAD 0xBE 0xEF → "deadbeef" (lowercase, no separator)
        assert srv.DataOpsClient._byte_array_to_hex(b"\xde\xad\xbe\xef") == "deadbeef"

    def test_preserves_length(self):
        data = bytes(range(256))
        assert len(srv.DataOpsClient._byte_array_to_hex(data)) == 512


class TestHmacSha256Hex:
    """``security:`` + uppercase hexdigest of HMAC-SHA256(key_bytes, message)."""

    def test_prefix_and_uppercase(self):
        key_hex = "0" * 64  # 32 zero bytes
        out = srv.DataOpsClient._hmac_sha256_hex("msg", key_hex)
        assert out.startswith("security:")
        assert out.split(":", 1)[1] == out.split(":", 1)[1].upper()

    def test_is_deterministic(self):
        key_hex = "00112233445566778899aabbccddeeff"
        assert srv.DataOpsClient._hmac_sha256_hex("msg", key_hex) == srv.DataOpsClient._hmac_sha256_hex("msg", key_hex)

    def test_different_keys_produce_different_signatures(self):
        assert srv.DataOpsClient._hmac_sha256_hex("m", "00" * 32) != srv.DataOpsClient._hmac_sha256_hex("m", "ff" * 32)


# ===========================================================================
# DataOpsClient.__init__ — input validation
# ===========================================================================


class TestDataOpsClientInit:
    """Construction-time validation of config."""

    def test_normalizes_source_type_lowercase(self):
        c = _make_client(source_type="Hive")
        # Use the internal field rather than guessing a public property
        assert c._source_type == "hive"

    def test_rejects_unknown_source_type(self):
        with pytest.raises(ValueError, match="DATAOPS_SOURCE_TYPE"):
            _make_client(source_type="postgres")

    def test_clickhouse_source_type_accepted(self):
        c = _make_client(source_type="ClickHouse")
        assert c._source_type == "clickhouse"

    def test_rejects_empty_base_url(self):
        with pytest.raises(ValueError, match="DATAOPS_BASE_URL"):
            _make_client(base_url="")

    def test_strips_trailing_slash_from_base_url(self):
        assert _make_client(base_url="https://x.com/openapi/")._base_url == "https://x.com/openapi"

    def test_timeout_floored_to_one(self):
        c = _make_client(timeout_sec=0)
        assert c._timeout_sec == 1


class TestAuthHeaders:
    """HMAC-SHA256 signing contract."""

    def test_contains_required_fields(self):
        c = _make_client()
        headers = c._build_auth_headers()
        for required in ("openApiSign", "timestamp", "appid", "random", "serviceInstanceId"):
            assert required in headers, f"missing {required}"
        assert headers["appid"] == "appid-test"
        assert headers["serviceInstanceId"] == "inst-1"
        assert headers["openApiSign"].startswith("security:")

    def test_timestamp_is_milliseconds(self):
        c = _make_client()
        h1 = c._build_auth_headers()
        h2 = c._build_auth_headers()
        # Two consecutive builds either share the same ms timestamp or differ
        # by at least 1 ms — never negative or non-integer.
        for ts in (h1["timestamp"], h2["timestamp"]):
            assert ts.isdigit()
            assert int(ts) > 1_000_000_000_000  # after year 2001 in ms

    def test_nonce_is_32_hex_chars(self):
        c = _make_client()
        headers = c._build_auth_headers()
        assert len(headers["random"]) == 32
        assert all(ch in "0123456789abcdef" for ch in headers["random"])

    def test_each_call_produces_fresh_nonce(self):
        c = _make_client()
        nonces = {c._build_auth_headers()["random"] for _ in range(5)}
        assert len(nonces) == 5  # 16 random bytes → collision-free


# ===========================================================================
# Pure response helpers
# ===========================================================================


class TestResponseBody:
    """_response_body unwraps common DataOps envelope layers."""

    def test_returns_payload_unchanged_when_dict_without_envelope(self):
        assert srv._response_body({"foo": 1}) == {"foo": 1}

    def test_unwraps_body_key(self):
        assert srv._response_body({"body": {"foo": 1}}) == {"foo": 1}

    def test_unwraps_data_key(self):
        assert srv._response_body({"data": {"foo": 1}}) == {"foo": 1}

    def test_body_preferred_over_data(self):
        # First-wins: code checks "body" before "data", so a payload with both
        # returns the body value.
        assert srv._response_body({"body": 1, "data": 2}) == 1

    def test_non_dict_passes_through(self):
        assert srv._response_body("plain") == "plain"
        assert srv._response_body(42) == 42
        assert srv._response_body(None) is None


class TestExtractQueryId:
    """_extract_queryId handles the three DataOps response shapes."""

    def test_extracts_from_dict_query_id(self):
        assert srv._extract_query_id({"queryId": "12345"}) == "12345"

    def test_strips_whitespace(self):
        assert srv._extract_query_id({"queryId": "  12345  "}) == "12345"

    def test_string_body_passthrough(self):
        # Some legacy endpoints return the query id as a plain string body.
        assert srv._extract_query_id("12345") == "12345"

    def test_numeric_body(self):
        assert srv._extract_query_id(12345) == "12345"

    def test_unwraps_envelope_body(self):
        assert srv._extract_query_id({"body": {"queryId": "q-1"}}) == "q-1"

    def test_missing_returns_empty_string(self):
        assert srv._extract_query_id({}) == ""
        assert srv._extract_query_id({"foo": "bar"}) == ""


class TestFindQueryResult:
    """_find_query_result locates a result record by queryId."""

    def test_finds_in_list_payload(self):
        payload = [
            {"queryId": "a", "status": "ok"},
            {"queryId": "b", "status": "ok"},
        ]
        assert srv._find_query_result(payload, "b") == {"queryId": "b", "status": "ok"}

    def test_returns_singleton_when_only_one_record(self):
        # Empty queryId in payload + single record → still matches (fallback).
        payload = {"queryId": "", "status": "ok"}
        assert srv._find_query_result(payload, "anything") == payload

    def test_returns_none_when_id_not_found(self):
        assert srv._find_query_result([{"queryId": "a"}], "b") is None

    def test_handles_non_dict_items(self):
        # Non-dict items are skipped silently.
        assert srv._find_query_result([1, "x", {"queryId": "a"}], "a") == {"queryId": "a"}


# ===========================================================================
# _status_payload — the most important mapping (5/6/7/10/running → vocabulary)
# ===========================================================================


class TestStatusPayload:
    """Map DataOps queryStatus integer to the resource lifecycle vocabulary."""

    def test_completed_status_5(self):
        out = srv._status_payload("j-1", {"queryStatus": 5})
        assert out["status"] == "completed"
        assert out["job_id"] == "j-1"
        assert out["exit_code"] == 0

    def test_failed_status_6_includes_error_message(self):
        out = srv._status_payload("j-1", {"queryStatus": 6, "errorMsg": "Table not found"})
        assert out["status"] == "failed"
        assert out["error"] == "Table not found"
        assert out["exit_code"] == 1

    def test_failed_status_6_includes_log_file_info(self):
        info = {"resultPath": "x", "headers": {"url": "https://obs/x"}}
        out = srv._status_payload("j-1", {"queryStatus": 6, "errorMsg": "oops", "logFileInfo": info})
        assert out["logFileInfo"] == info

    def test_failed_status_6_defaults_error_message(self):
        out = srv._status_payload("j-1", {"queryStatus": 6})
        assert out["error"] == "DataOps SQL validation failed"

    def test_cancelled_status_7(self):
        out = srv._status_payload("j-1", {"queryStatus": 7})
        assert out["status"] == "cancelled"
        assert out["exit_code"] == 130

    def test_cancelled_status_10(self):
        # 10 is treated the same as 7 (10 = skipped per the spec).
        out = srv._status_payload("j-1", {"queryStatus": 10})
        assert out["status"] == "cancelled"

    def test_running_status_666(self):
        out = srv._status_payload("j-1", {"queryStatus": 666})
        assert out["status"] == "running"
        assert "666" in out["summary"]

    def test_unknown_status_falls_through_to_failed(self):
        out = srv._status_payload("j-1", {"queryStatus": 999})
        assert out["status"] == "failed"
        assert "999" in out["error"]

    def test_invalid_status_type_returns_failed(self):
        # Non-integer, non-numeric queryStatus → failed with parse hint.
        out = srv._status_payload("j-1", {"queryStatus": "bogus"})
        assert out["status"] == "failed"
        assert "invalid queryStatus" in out["error"]


# ===========================================================================
# DataOpsClient HTTP methods — mocked httpx
# ===========================================================================


class TestClientSubmit:
    """submit() POSTs to /v3/adHoc/create and extracts the queryId."""

    def test_happy_path_returns_query_id(self):
        client = _make_client()
        with patch.object(srv.httpx, "post", return_value=_mock_post_response({"queryId": "q-1"})):
            assert client.submit("SELECT 1") == "q-1"

    def test_unwraps_body_envelope(self):
        client = _make_client()
        body = {"body": {"queryId": "q-2"}}
        with patch.object(srv.httpx, "post", return_value=_mock_post_response(body)):
            assert client.submit("SELECT 1") == "q-2"

    def test_error_string_response_raises_value_error(self):
        # DataOps sometimes returns the error message as a plain string body
        # (e.g. "Table 'x' already exists!"). Treat that as a failure.
        client = _make_client()
        with (
            patch.object(srv.httpx, "post", return_value=_mock_post_response("Table 'x' already exists!")),
            pytest.raises(ValueError, match="already exists"),
        ):
            client.submit("CREATE TABLE x (id INT)")

    def test_digit_string_query_id_accepted(self):
        # A string of digits is treated as a query id (DataOps sometimes returns
        # only digits). Verify we don't misinterpret it as an error.
        client = _make_client()
        with patch.object(srv.httpx, "post", return_value=_mock_post_response("12345")):
            assert client.submit("SELECT 1") == "12345"

    def test_missing_query_id_raises(self):
        client = _make_client()
        with (
            patch.object(srv.httpx, "post", return_value=_mock_post_response({"foo": "bar"})),
            pytest.raises(ValueError, match="no queryId"),
        ):
            client.submit("SELECT 1")

    def test_empty_sql_raises(self):
        client = _make_client()
        with pytest.raises(ValueError, match="required"):
            client.submit("   ")

    def test_http_error_raises(self):
        client = _make_client()
        with (
            patch.object(srv.httpx, "post", return_value=_mock_post_response({}, status_code=500)),
            pytest.raises(ValueError),
        ):
            client.submit("SELECT 1")

    def test_transport_error_raises(self):
        client = _make_client()
        with (
            patch.object(srv.httpx, "post", side_effect=srv.httpx.HTTPError("connection refused")),
            pytest.raises(ValueError, match="unreachable"),
        ):
            client.submit("SELECT 1")

    def test_request_body_contains_exec_user(self):
        """The exec_user field must be propagated to the request body."""
        client = _make_client(exec_user="bob")
        captured = {}

        def fake_post(url, content=None, headers=None, timeout=None):
            captured["body"] = json.loads(content)
            return _mock_post_response({"queryId": "q-1"})

        with patch.object(srv.httpx, "post", side_effect=fake_post):
            client.submit("SELECT 1")
        assert captured["body"]["execUser"] == "bob"


class TestClientQuery:
    """query() POSTs to /v3/adHoc/query and extracts the result item."""

    def test_single_record_response(self):
        client = _make_client()
        payload = {"queryId": "q-1", "queryStatus": 5}
        with patch.object(srv.httpx, "post", return_value=_mock_post_response(payload)):
            assert client.query("q-1") == payload

    def test_list_response_finds_target_item(self):
        client = _make_client()
        payload = [
            {"queryId": "q-1", "queryStatus": 5},
            {"queryId": "q-2", "queryStatus": 6},
        ]
        with patch.object(srv.httpx, "post", return_value=_mock_post_response(payload)):
            assert client.query("q-2") == {"queryId": "q-2", "queryStatus": 6}

    def test_missing_item_raises(self):
        client = _make_client()
        with (
            patch.object(srv.httpx, "post", return_value=_mock_post_response([{"queryId": "q-1"}])),
            pytest.raises(ValueError, match="no result"),
        ):
            client.query("q-missing")

    def test_empty_job_id_raises(self):
        client = _make_client()
        with pytest.raises(ValueError, match="required"):
            client.query("")


class TestClientGetJobLog:
    """get_job_log() pulls the logFileInfo URL and downloads from OBS."""

    def test_returns_log_content_on_success(self):
        client = _make_client()
        # 1) query() returns a record with logFileInfo
        # 2) httpx.get() returns the log content
        query_payload = {
            "queryId": "q-1",
            "logFileInfo": {
                "url": "https://obs/log.txt",
                "headers": {"Authorization": "AWS4 xyz"},
            },
        }
        with patch.object(srv.httpx, "post", return_value=_mock_post_response(query_payload)):
            obs_resp = MagicMock(status_code=200, text="log body here")
            with patch.object(srv.httpx, "get", return_value=obs_resp):
                out = client.get_job_log("q-1")
        assert out["log_content"] == "log body here"
        assert out["status_code"] == 200

    def test_returns_error_when_log_info_missing_url(self):
        client = _make_client()
        with patch.object(srv.httpx, "post", return_value=_mock_post_response({"queryId": "q-1"})):
            out = client.get_job_log("q-1")
        assert "error" in out
        assert "No logFileInfo URL" in out["error"]

    def test_returns_error_when_log_info_missing_headers(self):
        client = _make_client()
        payload = {"queryId": "q-1", "logFileInfo": {"url": "https://obs/x"}}
        with patch.object(srv.httpx, "post", return_value=_mock_post_response(payload)):
            out = client.get_job_log("q-1")
        assert "auth headers" in out["error"]

    def test_returns_error_on_http_4xx(self):
        client = _make_client()
        payload = {
            "queryId": "q-1",
            "logFileInfo": {"url": "https://obs/x", "headers": {"Authorization": "x"}},
        }
        with patch.object(srv.httpx, "post", return_value=_mock_post_response(payload)):
            obs_resp = MagicMock(status_code=403, text="Forbidden")
            with patch.object(srv.httpx, "get", return_value=obs_resp):
                out = client.get_job_log("q-1")
        assert "HTTP 403" in out["error"]

    def test_returns_error_on_observe_network_error(self):
        client = _make_client()
        payload = {
            "queryId": "q-1",
            "logFileInfo": {"url": "https://obs/x", "headers": {"Authorization": "x"}},
        }
        with (
            patch.object(srv.httpx, "post", return_value=_mock_post_response(payload)),
            patch.object(srv.httpx, "get", side_effect=srv.httpx.HTTPError("timeout")),
        ):
            out = client.get_job_log("q-1")
        assert "Failed to fetch" in out["error"]


# ===========================================================================
# MCP tool surface — submit_job / poll_job / collect_job / etc.
# ===========================================================================


class TestMcpTools:
    """End-to-end behavior of the FastMCP tools registered by create_server."""

    def _make_server(self) -> tuple[Any, MagicMock]:
        """Build a server with a mocked client and return (server, client_mock)."""
        client = MagicMock(spec=srv.DataOpsClient)
        return srv.create_server(host="127.0.0.1", port=8767, client=client), client

    def _call(self, server, tool_name: str, arguments: dict) -> dict[str, Any]:
        """Invoke a FastMCP tool.

        FastMCP ``call_tool`` returns ``(content_blocks, structured_dict)``;
        the structured dict is the canonical format for JSON tools.
        """
        import asyncio as _aio

        content, structured = _aio.run(server.call_tool(tool_name, arguments))
        return structured or _from_text_content(content)

    # --- submit_job ---------------------------------------------------------

    def test_submit_job_returns_running_with_job_id(self):
        server, client = self._make_server()
        client.submit.return_value = "q-1"
        payload = self._call(
            server,
            "submit_job",
            {"envelope": {"command": "SELECT 1"}, "allocation": {}},
        )
        assert payload["status"] == "running"
        assert payload["job_id"] == "q-1"
        client.submit.assert_called_once_with("SELECT 1")

    def test_submit_job_empty_sql_returns_failed(self):
        server, _ = self._make_server()
        payload = self._call(
            server,
            "submit_job",
            {"envelope": {"command": "  "}, "allocation": {}},
        )
        assert payload["status"] == "failed"
        assert "empty" in payload["error"]

    def test_submit_job_client_error_returns_failed(self):
        server, client = self._make_server()
        client.submit.side_effect = ValueError("upstream boom")
        payload = self._call(
            server,
            "submit_job",
            {"envelope": {"command": "SELECT 1"}, "allocation": {}},
        )
        assert payload["status"] == "failed"
        assert "upstream boom" in payload["error"]

    # --- poll_job -----------------------------------------------------------

    def test_poll_job_completed(self):
        server, client = self._make_server()
        client.query.return_value = {"queryId": "q-1", "queryStatus": 5}
        payload = self._call(server, "poll_job", {"job_id": "q-1"})
        assert payload["status"] == "completed"
        assert payload["job_id"] == "q-1"

    def test_poll_job_failed(self):
        server, client = self._make_server()
        client.query.return_value = {
            "queryId": "q-1",
            "queryStatus": 6,
            "errorMsg": "Table not found",
        }
        payload = self._call(server, "poll_job", {"job_id": "q-1"})
        assert payload["status"] == "failed"
        assert "Table not found" in payload["error"]
        # ``logFileInfo`` is only emitted when queryStatus == 6 *and* the result
        # contains a non-empty logFileInfo field; here we passed none.
        assert "logFileInfo" not in payload

    def test_poll_job_client_error(self):
        server, client = self._make_server()
        client.query.side_effect = ValueError("network down")
        payload = self._call(server, "poll_job", {"job_id": "q-1"})
        assert payload["status"] == "failed"
        assert "network down" in payload["error"]

    # --- collect_job --------------------------------------------------------

    def test_collect_job_completed_attaches_summary(self):
        server, client = self._make_server()
        client.query.return_value = {"queryId": "q-1", "queryStatus": 5}
        payload = self._call(server, "collect_job", {"job_id": "q-1"})
        assert payload["status"] == "completed"
        assert payload["summary"] == "SQL validated successfully by DataOps"

    def test_collect_job_failed_carries_log_info(self):
        server, client = self._make_server()
        client.query.return_value = {
            "queryId": "q-1",
            "queryStatus": 6,
            "errorMsg": "Table not found",
            "logFileInfo": {"url": "https://obs/x", "headers": {}},
        }
        payload = self._call(server, "collect_job", {"job_id": "q-1"})
        assert payload["status"] == "failed"
        assert payload["logFileInfo"] == {"url": "https://obs/x", "headers": {}}

    def test_collect_job_client_error(self):
        server, client = self._make_server()
        client.query.side_effect = ValueError("boom")
        payload = self._call(server, "collect_job", {"job_id": "q-1"})
        assert payload["status"] == "failed"
        assert "boom" in payload["error"]

    # --- cancel_job ---------------------------------------------------------

    def test_cancel_job_always_returns_error(self):
        server, _ = self._make_server()
        payload = self._call(server, "cancel_job", {"job_id": "q-1"})
        assert payload["status"] == "error"
        assert "not configured" in payload["error"]
        assert payload["exit_code"] == 1

    # --- get_job_log --------------------------------------------------------

    def test_get_job_log_success(self):
        server, client = self._make_server()
        client.get_job_log.return_value = {"log_content": "execution log here"}
        payload = self._call(server, "get_job_log", {"job_id": "q-1"})
        assert payload["status"] == "success"
        assert payload["log_content"] == "execution log here"

    def test_get_job_log_obs_error(self):
        server, client = self._make_server()
        client.get_job_log.return_value = {"error": "OBS 403"}
        payload = self._call(server, "get_job_log", {"job_id": "q-1"})
        assert payload["status"] == "error"
        assert "OBS 403" in payload["error"]

    def test_get_job_log_value_error(self):
        server, client = self._make_server()
        client.get_job_log.side_effect = ValueError("query failed")
        payload = self._call(server, "get_job_log", {"job_id": "q-1"})
        assert payload["status"] == "error"
        assert "query failed" in payload["error"]


def _from_text_content(blocks: list[Any]) -> dict[str, Any]:
    """Fallback: parse the first ``TextContent`` block as JSON."""
    if not blocks:
        return {}
    text = getattr(blocks[0], "text", None) or (blocks[0].get("text") if isinstance(blocks[0], dict) else None)
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw_text": text}


# ===========================================================================
# Module-level config helpers
# ===========================================================================


class TestBuildClient:
    """_build_client() reads env vars and instantiates DataOpsClient."""

    def test_prefers_hive_resource_name_when_source_is_hive(self, monkeypatch):
        monkeypatch.setenv("DATAOPS_BASE_URL", "https://x/openapi")
        monkeypatch.setenv("DATAOPS_APPID", "a")
        monkeypatch.setenv("DATAOPS_SECRET_KEY", "00" * 32)
        monkeypatch.setenv("DATAOPS_SERVICE_INSTANCE_ID", "i")
        monkeypatch.setenv("DATAOPS_HIVE_RESOURCE_NAME", "hive-cluster")
        monkeypatch.setenv("DATAOPS_CLICKHOUSE_RESOURCE_NAME", "ch-cluster")
        monkeypatch.setenv("DATAOPS_EXEC_USER", "alice")
        monkeypatch.setenv("DATAOPS_SOURCE_TYPE", "hive")
        client = srv._build_client()
        assert client._resource_name == "hive-cluster"
        assert client._source_type == "hive"

    def test_prefers_clickhouse_resource_name_when_source_is_clickhouse(self, monkeypatch):
        monkeypatch.setenv("DATAOPS_BASE_URL", "https://x/openapi")
        monkeypatch.setenv("DATAOPS_APPID", "a")
        monkeypatch.setenv("DATAOPS_SECRET_KEY", "00" * 32)
        monkeypatch.setenv("DATAOPS_SERVICE_INSTANCE_ID", "i")
        monkeypatch.setenv("DATAOPS_HIVE_RESOURCE_NAME", "hive-cluster")
        monkeypatch.setenv("DATAOPS_CLICKHOUSE_RESOURCE_NAME", "ch-cluster")
        monkeypatch.setenv("DATAOPS_SOURCE_TYPE", "clickhouse")
        client = srv._build_client()
        assert client._resource_name == "ch-cluster"
        assert client._source_type == "clickhouse"

    def test_default_source_type_is_hive(self, monkeypatch):
        monkeypatch.delenv("DATAOPS_SOURCE_TYPE", raising=False)
        for k in [
            "DATAOPS_BASE_URL",
            "DATAOPS_APPID",
            "DATAOPS_SECRET_KEY",
            "DATAOPS_SERVICE_INSTANCE_ID",
            "DATAOPS_HIVE_RESOURCE_NAME",
        ]:
            monkeypatch.setenv(k, "x")
        client = srv._build_client()
        assert client._source_type == "hive"


class TestParseArgs:
    """_parse_args() reads CLI args and falls back to env vars."""

    def test_defaults_to_env_vars(self, monkeypatch):
        monkeypatch.setenv("DATAOPS_MCP_HOST", "10.0.0.1")
        monkeypatch.setenv("DATAOPS_MCP_PORT", "9000")
        monkeypatch.setattr("sys.argv", ["prog"])
        args = srv._parse_args()
        assert args.host == "10.0.0.1"
        assert args.port == 9000

    def test_cli_overrides_env(self, monkeypatch):
        monkeypatch.setenv("DATAOPS_MCP_HOST", "10.0.0.1")
        monkeypatch.setenv("DATAOPS_MCP_PORT", "9000")
        monkeypatch.setattr("sys.argv", ["prog", "--host", "127.0.0.1", "--port", "1234"])
        args = srv._parse_args()
        assert args.host == "127.0.0.1"
        assert args.port == 1234

    def test_default_no_env(self, monkeypatch):
        # argparse picks up DATAOPS_MCP_HOST/PORT inside the function, so we
        # need to clear them — but they have built-in defaults. Verify the
        # default fallback path.
        monkeypatch.delenv("DATAOPS_MCP_HOST", raising=False)
        monkeypatch.delenv("DATAOPS_MCP_PORT", raising=False)
        monkeypatch.setattr("sys.argv", ["prog"])
        args = srv._parse_args()
        assert args.host == "0.0.0.0"
        assert args.port == 8767
