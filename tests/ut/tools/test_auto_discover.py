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
"""Tests that per-Agent ToolManager auto-discovers MCP/A2A tools after init_from_config."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.types import Tool as MCPTool

from dataagent.actions.tools.mcp import MCPClientWrapper, MCPServerConfig, MCPToolWrapper
from dataagent.core.managers.action_manager.manager import ToolManager


def _a2a_tools_config() -> dict[str, Any]:
    """Minimal YAML-shaped config with one A2A agent endpoint."""
    return {
        "TOOLS": {
            "A2A": [
                {
                    "my_server": {
                        "base_url": "http://localhost:9999",
                        "auth_token": "sk_test",
                        "timeout": 30,
                    },
                },
            ],
        },
    }


def _mcp_tools_config() -> dict[str, Any]:
    """Minimal YAML-shaped config with one MCP stdio server."""
    return {
        "TOOLS": {
            "mcp_servers": [
                {
                    "server_id": "nl2sql",
                    "transport_type": "stdio",
                    "config": {
                        "command": "python",
                        "args": ["-m", "dataagent.actions.tools.mcp_tool.nl2sql"],
                        "env": {},
                    },
                    "description": "自然语言转SQL工具服务器",
                },
            ],
        },
    }


def test_init_from_config_triggers_auto_discover_for_a2a(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: TOOLS.A2A registration must trigger enable_auto_discover (Flex per-Agent path)."""
    discover_calls: list[str] = []

    def _track_discover(self: ToolManager) -> None:
        discover_calls.append("discover_all_sync")

    monkeypatch.setattr(ToolManager, "_discover_all_sync", _track_discover)

    tool_manager = ToolManager()
    tool_manager.init_from_config(_a2a_tools_config())

    assert tool_manager.is_auto_discover_enabled()
    assert discover_calls == ["discover_all_sync"]
    assert "my_server" in tool_manager._registered_a2a_agents


def test_init_from_config_triggers_auto_discover_for_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard: TOOLS.mcp_servers registration must trigger enable_auto_discover (Flex per-Agent path)."""
    discover_calls: list[str] = []

    def _track_discover(self: ToolManager) -> None:
        discover_calls.append("discover_all_sync")

    monkeypatch.setattr(ToolManager, "_discover_all_sync", _track_discover)

    tool_manager = ToolManager()
    tool_manager.init_from_config(_mcp_tools_config())

    assert tool_manager.is_auto_discover_enabled()
    assert discover_calls == ["discover_all_sync"]
    assert "nl2sql" in tool_manager._registered_mcp_servers


def test_init_from_config_skips_auto_discover_without_remote_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remote auto-discover should not run when no MCP/A2A endpoints are configured."""
    discover_calls: list[str] = []

    def _track_discover(self: ToolManager) -> None:
        discover_calls.append("discover_all_sync")

    monkeypatch.setattr(ToolManager, "_discover_all_sync", _track_discover)

    tool_manager = ToolManager()
    tool_manager.init_from_config({"AGENT_CONFIG": {"name": "unit-test-agent"}})

    assert not tool_manager.is_auto_discover_enabled()
    assert discover_calls == []


def test_init_from_config_registers_discovered_a2a_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovered A2A tools must land in _tool_instances for Planner bind_tools."""
    from dataagent.actions.tools.a2a import A2AClientWrapper, A2AToolWrapper, AgentConfig

    def _sync_discover_all(self: ToolManager) -> None:
        for agent_id in self._registered_a2a_agents:
            client = A2AClientWrapper(
                AgentConfig(agent_id=agent_id, base_url="http://localhost:9999", auth_token="sk_test"),
            )
            tool = A2AToolWrapper(
                client,
                "chat",
                {
                    "name": "chat",
                    "description": "Interactive conversational data analysis",
                    "parameters": {
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                        "required": ["message"],
                    },
                },
            )
            self._tool_instances[tool.name] = tool
            self._tool_schemas[tool.name] = tool.get_schema()

    monkeypatch.setattr(ToolManager, "_discover_all_sync", _sync_discover_all)

    tool_manager = ToolManager()
    tool_manager.init_from_config(_a2a_tools_config())

    assert tool_manager.is_auto_discover_enabled()
    assert tool_manager.exists("chat")
    assert "chat" in [t.name for t in tool_manager.get_all_tool_instances()]


def test_init_from_config_registers_discovered_mcp_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovered MCP tools must land in _tool_instances for Planner bind_tools."""

    def _sync_discover_all(self: ToolManager) -> None:
        for server_id in self._registered_mcp_servers:
            client = MCPClientWrapper(
                MCPServerConfig.create_stdio_config(server_id, "python", ["-m", "dummy_mcp_server"]),
            )
            mcp_tool = MCPTool(
                name="nl2sql",
                description="Query the database using natural language",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                },
            )
            tool = MCPToolWrapper(client, mcp_tool)
            self._tool_instances[tool.name] = tool
            self._tool_schemas[tool.name] = tool.get_schema()

    monkeypatch.setattr(ToolManager, "_discover_all_sync", _sync_discover_all)

    tool_manager = ToolManager()
    tool_manager.init_from_config(_mcp_tools_config())

    assert tool_manager.is_auto_discover_enabled()
    assert tool_manager.exists("nl2sql")
    assert "nl2sql" in [t.name for t in tool_manager.get_all_tool_instances()]
