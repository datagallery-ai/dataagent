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
"""Integration tests: MCP/A2A hooks from YAML attach to discovered tool instances."""

from __future__ import annotations

from typing import Any

import pytest
from dataagent.actions.tools.hooks.examples import example_hooks
from dataagent.actions.tools.mcp import MCPClientWrapper, MCPServerConfig, MCPToolWrapper
from dataagent.core.managers.action_manager.manager import ToolManager
from mcp.types import Tool as MCPTool

from dataagent.actions.tools.a2a import A2AClientWrapper, A2AToolWrapper, AgentConfig

_HOOK_PRE = "dataagent.actions.tools.hooks.examples.example_hooks.audit_pre"
_HOOK_POST = "dataagent.actions.tools.hooks.examples.example_hooks.audit_post"


def _hooks_block() -> dict[str, list[str]]:
    return {"pre": [_HOOK_PRE], "post": [_HOOK_POST]}


def _mcp_tools_config() -> dict[str, Any]:
    return {
        "TOOLS": {
            "mcp_servers": [
                {
                    "server_id": "test_mcp",
                    "transport_type": "stdio",
                    "config": {
                        "command": "python",
                        "args": ["-m", "dummy"],
                        "env": {},
                    },
                    "hooks": _hooks_block(),
                },
            ],
        },
    }


def _a2a_tools_config() -> dict[str, Any]:
    return {
        "TOOLS": {
            "A2A": [
                {
                    "test_a2a": {
                        "base_url": "http://127.0.0.1:9999",
                        "auth_token": None,
                        "timeout": 30,
                        "hooks": _hooks_block(),
                    },
                },
            ],
        },
    }


def _init_tool_manager(
    config: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    discover_impl,
) -> ToolManager:
    """Build ToolManager from config with a stubbed ``_discover_all_sync``."""
    monkeypatch.setattr(ToolManager, "_discover_all_sync", discover_impl)
    tool_manager = ToolManager()
    tool_manager.init_from_config(config)
    return tool_manager


def _noop_discover_all(_self: ToolManager) -> None:
    return None


def _fake_mcp_tool(server_id: str, tool_name: str = "query_db") -> MCPToolWrapper:
    client = MCPClientWrapper(
        MCPServerConfig.create_stdio_config(server_id, "python", ["-m", "dummy"]),
    )
    mcp_tool = MCPTool(
        name=tool_name,
        description="Example MCP tool",
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )
    return MCPToolWrapper(client, mcp_tool)


def _fake_a2a_tool(agent_id: str, tool_name: str = "chat") -> A2AToolWrapper:
    client = A2AClientWrapper(
        AgentConfig(agent_id=agent_id, base_url="http://127.0.0.1:9999"),
    )
    return A2AToolWrapper(
        client,
        tool_name,
        {
            "name": tool_name,
            "description": "Example A2A tool",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
    )


def test_mcp_server_hooks_parsed_into_registry_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """``init_from_config`` stores server-level hook callables in ``_mcp_server_hooks``."""
    tool_manager = _init_tool_manager(_mcp_tools_config(), monkeypatch, discover_impl=_noop_discover_all)
    hook_lists = tool_manager._mcp_server_hooks["test_mcp"]
    assert hook_lists.pre[0] is example_hooks.audit_pre
    assert hook_lists.post[0] is example_hooks.audit_post


def test_a2a_agent_hooks_parsed_into_registry_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """``init_from_config`` stores agent-level hook callables in ``_a2a_agent_hooks``."""
    tool_manager = _init_tool_manager(_a2a_tools_config(), monkeypatch, discover_impl=_noop_discover_all)
    hook_lists = tool_manager._a2a_agent_hooks["test_a2a"]
    assert hook_lists.pre[0] is example_hooks.audit_pre
    assert hook_lists.post[0] is example_hooks.audit_post


@pytest.mark.asyncio
async def test_discover_mcp_tools_attaches_hooks_to_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    """``discover_mcp_tools`` attaches configured pre/post hooks to each discovered tool."""

    class _FakeMcpRegistry:
        async def list_server_tools(self, server_id: str) -> list[MCPToolWrapper]:
            return [_fake_mcp_tool(server_id, "query_db")]

    tool_manager = _init_tool_manager(_mcp_tools_config(), monkeypatch, discover_impl=_noop_discover_all)
    tool_manager._mcp_registry = _FakeMcpRegistry()  # type: ignore[assignment]

    names = await tool_manager.discover_mcp_tools("test_mcp")
    assert names == ["query_db"]

    tool = tool_manager.get("query_db")
    assert tool.pre_hooks[0] is example_hooks.audit_pre
    assert tool.post_hooks[0] is example_hooks.audit_post


@pytest.mark.asyncio
async def test_discover_a2a_tools_attaches_hooks_to_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    """``discover_a2a_tools`` attaches configured pre/post hooks to each discovered tool."""

    class _FakeA2aRegistry:
        async def list_agent_tools(self, agent_id: str) -> list[A2AToolWrapper]:
            return [_fake_a2a_tool(agent_id, "chat")]

    tool_manager = _init_tool_manager(_a2a_tools_config(), monkeypatch, discover_impl=_noop_discover_all)
    tool_manager._a2a_registry = _FakeA2aRegistry()  # type: ignore[assignment]

    names = await tool_manager.discover_a2a_tools("test_a2a")
    assert names == ["chat"]

    tool = tool_manager.get("chat")
    assert tool.pre_hooks[0] is example_hooks.audit_pre
    assert tool.post_hooks[0] is example_hooks.audit_post


def test_init_from_config_auto_discover_attaches_mcp_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full ``_discover_all_sync`` path attaches MCP server hooks like production init."""

    def _sync_discover_all(self: ToolManager) -> None:
        for server_id in self._registered_mcp_servers:
            tool = _fake_mcp_tool(server_id, "nl2sql")
            hook_lists = self._mcp_server_hooks.get(server_id)
            if hook_lists is not None:
                from dataagent.actions.tools.hooks.config import attach_hooks_to_tool

                attach_hooks_to_tool(tool, hook_lists)
            self._tool_instances[tool.name] = tool
            self._tool_schemas[tool.name] = tool.get_schema()

    tool_manager = _init_tool_manager(_mcp_tools_config(), monkeypatch, discover_impl=_sync_discover_all)
    tool = tool_manager.get("nl2sql")
    assert tool.pre_hooks[0] is example_hooks.audit_pre
    assert tool.post_hooks[0] is example_hooks.audit_post


def test_init_from_config_auto_discover_attaches_a2a_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full ``_discover_all_sync`` path attaches A2A agent hooks like production init."""

    def _sync_discover_all(self: ToolManager) -> None:
        for agent_id in self._registered_a2a_agents:
            tool = _fake_a2a_tool(agent_id, "chat")
            hook_lists = self._a2a_agent_hooks.get(agent_id)
            if hook_lists is not None:
                from dataagent.actions.tools.hooks.config import attach_hooks_to_tool

                attach_hooks_to_tool(tool, hook_lists)
            self._tool_instances[tool.name] = tool
            self._tool_schemas[tool.name] = tool.get_schema()

    tool_manager = _init_tool_manager(_a2a_tools_config(), monkeypatch, discover_impl=_sync_discover_all)
    tool = tool_manager.get("chat")
    assert tool.pre_hooks[0] is example_hooks.audit_pre
    assert tool.post_hooks[0] is example_hooks.audit_post


def test_local_function_hooks_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """``TOOLS.local_functions[].hooks`` attaches to the registered local tool instance."""
    config = {
        "TOOLS": {
            "local_functions": [
                {
                    "module": "dataagent.actions.tools.hooks.examples.example_hooks",
                    "function": "noop_probe_tool",
                    "name": "noop_probe_with_hooks",
                    "hooks": _hooks_block(),
                },
            ],
        },
    }
    tool_manager = _init_tool_manager(config, monkeypatch, discover_impl=_noop_discover_all)
    # ``function`` wins over YAML ``name`` in ToolManager._register_local_tools
    tool = tool_manager.get("noop_probe_tool")
    assert tool.pre_hooks[0] is example_hooks.audit_pre
    assert tool.post_hooks[0] is example_hooks.audit_post
