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
"""Unit tests for MCP resource submit-time preflight probing."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from dataagent.actions.tools.mcp import MCPClientWrapper, MCPServerConfig
from dataagent.core.resource_runtime.mcp import (
    probe_mcp_reachability_sync,
    resolve_mcp_preflight_timeout_sec,
)
from dataagent.resources.catalog.models import Resource
from dataagent.resources.resolve.prepare import DriverBinding
from dataagent.utils.constants import DEFAULT_MCP_PREFLIGHT_TIMEOUT_SEC


def _resource_with_preflight(**transport_overrides: object) -> Resource:
    """Build one MCP resource for preflight timeout tests."""
    transport: dict[str, Any] = {
        "type": "mcp",
        "url": "http://127.0.0.1:8766/mcp",
        **transport_overrides,
    }
    return Resource(
        id="clickhouse_pool",
        name="clickhouse_pool",
        category="executable",
        capacity=4,
        unit="slot",
        consumption={"*": 1},
        operations={
            "submit": "submit_job",
            "poll": "poll_job",
            "collect": "collect_job",
            "cancel": "cancel_job",
        },
        transport=transport,
    )


def test_resolve_mcp_preflight_timeout_sec_defaults_to_five():
    """Preflight timeout defaults to five seconds and respects transport cap."""
    resource = _resource_with_preflight()
    driver = DriverBinding(
        transport_type="mcp",
        operation_ids={"submit": "submit_job"},
        mcp_url="http://127.0.0.1:8766/mcp",
        mcp_timeout_sec=30,
    )
    assert resolve_mcp_preflight_timeout_sec(resource, driver) == DEFAULT_MCP_PREFLIGHT_TIMEOUT_SEC


def test_resolve_mcp_preflight_timeout_sec_honors_config_and_cap():
    """Configured preflight timeout is capped by the transport timeout."""
    resource = _resource_with_preflight(preflight_timeout_sec=12)
    driver = DriverBinding(
        transport_type="mcp",
        operation_ids={"submit": "submit_job"},
        mcp_url="http://127.0.0.1:8766/mcp",
        mcp_timeout_sec=8,
    )
    assert resolve_mcp_preflight_timeout_sec(resource, driver) == 8


def test_probe_mcp_reachability_sync_returns_none_when_ping_succeeds():
    """Successful ping yields no preflight error."""
    client = MCPClientWrapper(
        MCPServerConfig(
            server_id="resource:clickhouse_pool",
            transport_type="streamable_http",
            config={"url": "http://127.0.0.1:8766/mcp", "timeout": 30},
        )
    )
    with patch.object(client, "ping", new=AsyncMock(return_value=True)):
        assert probe_mcp_reachability_sync(client, timeout_sec=2) is None


def test_probe_mcp_reachability_sync_returns_error_when_ping_fails():
    """Failed ping yields a formatted unreachable error."""
    client = MCPClientWrapper(
        MCPServerConfig(
            server_id="resource:clickhouse_pool",
            transport_type="streamable_http",
            config={"url": "http://127.0.0.1:8766/mcp", "timeout": 30},
        )
    )
    with patch.object(client, "ping", new=AsyncMock(side_effect=ConnectionError("connection refused"))):
        message = probe_mcp_reachability_sync(client, timeout_sec=2)
    assert message is not None
    assert "MCP server unreachable" in message
    assert "preflight" in message
