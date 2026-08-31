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
import os

import pytest

from dataagent.core.managers.action_manager.manager import ToolManager

tool_manager = ToolManager()


@pytest.mark.asyncio
async def test_mcp_tool_external_tavily():
    tool_manager.register_mcp_server(
        server_id="mcpServers",
        transport_type="stdio",
        config={
            "command": "npx",
            "args": ["-y", "tavily-mcp@0.1.4"],
            "env": {"TAVILY_API_KEY": os.getenv("TAVILY_API_KEY", "")},
        },
    )

    # 检查MCP服务器是否已注册
    servers = tool_manager.mcp_registry.list_servers()
    assert "mcpServers" in servers

    # 尝试发现工具（可能会因为网络/依赖问题失败，真实集成环境下应稳定）
    tools = await tool_manager.discover_mcp_tools("mcpServers")
    assert tools is not None

    await tool_manager.cleanup()
