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
"""Unit tests for resource MCP driver helpers."""

from __future__ import annotations

import pytest
from mcp.types import CallToolResult, TextContent

from dataagent.core.resource_runtime.mcp import (
    format_mcp_call_exception,
    mcp_server_config_from_binding,
    normalize_mcp_call_tool_result,
)
from dataagent.resources.drivers.mcp_resource import (
    resolve_mcp_transport,
    resolve_secret_ref,
    resolve_transport_headers,
)
from dataagent.resources.resolve.prepare import DriverBinding


def test_resolve_secret_ref_reads_env(monkeypatch):
    """secret_ref env: resolves from process environment."""
    monkeypatch.setenv("COMPUTE_TOKEN", "token-123")
    assert resolve_secret_ref({"secret_ref": "env:COMPUTE_TOKEN"}) == "token-123"


def test_resolve_secret_ref_missing_env_raises(monkeypatch):
    """Missing env var fails fast."""
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    with pytest.raises(ValueError, match="environment variable is not set"):
        resolve_secret_ref({"secret_ref": "env:MISSING_TOKEN"})


def test_resolve_transport_headers_plain_and_secret(monkeypatch):
    """Headers may mix plain strings and secret_ref objects."""
    monkeypatch.setenv("COMPUTE_TOKEN", "abc")
    resolved = resolve_transport_headers(
        {
            "X-Plain": "plain",
            "Authorization": {"secret_ref": "env:COMPUTE_TOKEN"},
        }
    )
    assert resolved == {"X-Plain": "plain", "Authorization": "abc"}


def test_resolve_mcp_transport_builds_plain_connection_fields():
    """Resource MCP transport resolves to plain url/headers fields."""
    resolved = resolve_mcp_transport(
        "compute_pool",
        {
            "type": "mcp",
            "url": "https://compute.example.com/mcp",
            "headers": {"Authorization": "token"},
            "timeout_sec": 45,
        },
    )
    assert resolved["url"] == "https://compute.example.com/mcp"
    assert resolved["headers"]["Authorization"] == "token"
    assert resolved["timeout_sec"] == 45
    config = mcp_server_config_from_binding(
        "compute_pool",
        DriverBinding(
            transport_type="mcp",
            operation_ids={},
            mcp_url=resolved["url"],
            mcp_headers=resolved["headers"],
            mcp_timeout_sec=resolved["timeout_sec"],
        ),
    )
    assert config.server_id == "resource:compute_pool"
    assert config.transport_type == "streamable_http"
    assert config.config["url"] == "https://compute.example.com/mcp"
    assert config.config["headers"]["Authorization"] == "token"
    assert config.config["timeout"] == 45


def test_normalize_mcp_call_tool_result_parses_json_text():
    """Text JSON content becomes an operation result dict."""
    payload = CallToolResult(
        content=[TextContent(type="text", text='{"status": "running", "job_id": "remote-1"}')],
        isError=False,
    )
    normalized = normalize_mcp_call_tool_result(payload)
    assert normalized == {"status": "running", "job_id": "remote-1"}


def test_normalize_mcp_call_tool_result_uses_structured_content():
    """structuredContent takes precedence when present."""
    payload = CallToolResult(
        structuredContent={"status": "completed", "summary": "done"},
        content=[],
        isError=False,
    )
    normalized = normalize_mcp_call_tool_result(payload)
    assert normalized == {"status": "completed", "summary": "done"}


def test_format_mcp_call_exception_marks_unreachable_errors():
    """Connection-style MCP failures include endpoint context."""
    from dataagent.actions.tools.mcp import MCPClientWrapper, MCPServerConfig

    client = MCPClientWrapper(
        MCPServerConfig(
            server_id="resource:clickhouse_pool",
            transport_type="streamable_http",
            config={"url": "http://127.0.0.1:8766/mcp"},
        )
    )
    message = format_mcp_call_exception(
        client,
        "submit_job",
        Exception("unhandled errors in a TaskGroup (1 sub-exception)"),
    )
    assert "MCP server unreachable" in message
    assert "submit_job" in message
