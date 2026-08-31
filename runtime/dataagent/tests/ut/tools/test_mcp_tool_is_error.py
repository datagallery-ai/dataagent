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
"""Flex MCP CallToolResult.isError must surface as a failed ToolResult / ToolMessage."""

from __future__ import annotations

import asyncio

import pytest
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool

from dataagent.actions.tools.mcp import MCPClientWrapper, MCPServerConfig, MCPToolWrapper
from dataagent.core.errors import DataAgentError
from dataagent.core.flex.nodes.executor import Executor
from dataagent.core.managers.action_manager.base import ToolResult


class _ErrorMcpClient(MCPClientWrapper):
    """Return a protocol-level MCP error without raising."""

    def __init__(self) -> None:
        super().__init__(MCPServerConfig.create_stdio_config("demo", "true"))

    async def call_tool(self, name: str, arguments: dict) -> CallToolResult:
        return CallToolResult(
            content=[TextContent(type="text", text="upstream tool exploded")],
            isError=True,
        )


def _wrapper() -> MCPToolWrapper:
    return MCPToolWrapper(
        _ErrorMcpClient(),
        MCPTool(name="boom", description="x", inputSchema={"type": "object", "properties": {}}),
    )


def test_mcp_is_error_returns_failed_tool_result() -> None:
    wrapper = _wrapper()
    with pytest.raises(DataAgentError) as caught:
        asyncio.run(wrapper._async_call())
    error = caught.value
    result = ToolResult(error=error)
    assert result.success is False
    assert error.source == "tool"
    assert "upstream tool exploded" in error.fact


def test_mcp_is_error_becomes_tool_message_error() -> None:
    wrapper = _wrapper()
    with pytest.raises(DataAgentError) as caught:
        asyncio.run(wrapper._async_call())
    result = ToolResult(error=caught.value)
    executor = Executor("executor")
    execution = executor._normalize_tool_execution(
        tool_name="boom",
        tool_call_id="call-1",
        tool_args={},
        result=result,
        metadata={},
    )
    message = executor._build_tool_message(execution)
    assert message.status == "error"
    assert "upstream tool exploded" in str(message.content)
