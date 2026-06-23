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
import sys

import pytest
from dataagent.core.managers.action_manager.manager import ToolManager

tool_manager = ToolManager()

# NOTE:
# In some environments `dataagent` is built/installed with Cython, and `mcp_examples` becomes an extension module.
# Running `python -m dataagent.actions.tools.mcp_examples` then fails with:
#   "No code object available for dataagent.actions.tools.mcp_examples"
# Use `-c` to start the server instead, which works for both pure-Python and compiled installs.
_MCP_DEMO_ARGS = [
    "-c",
    "from dataagent.actions.tools.mcp_examples import run_stdio_server; run_stdio_server()",
]


@pytest.mark.asyncio
async def test_mcp_tool_register_by_sdk():
    # 注册MCP工具（子进程必须用当前解释器，否则找不到已安装的 dataagent）
    tool_manager.register_mcp_server(
        server_id="demo_server",
        transport_type="stdio",
        config={"command": sys.executable, "args": list(_MCP_DEMO_ARGS)},
    )

    tools = await tool_manager.discover_mcp_tools("demo_server")
    assert len(tools) == 3
    assert "calculate_sum" in tools
    assert "get_file_info" in tools
    assert "list_directory" in tools

    await tool_manager.cleanup()


@pytest.mark.asyncio
async def test_mcp_tool_register_by_sdk2():
    # 注册MCP工具
    tool_manager.register_mcp_server(
        server_id="demo_server2",
        transport_type="stdio",
        config={"command": sys.executable, "args": list(_MCP_DEMO_ARGS)},
    )

    # 检查MCP服务器是否已注册
    servers = tool_manager.mcp_registry.list_servers()
    assert "demo_server2" in servers

    # 发现工具
    tools = await tool_manager.discover_mcp_tools("demo_server2")
    assert len(tools) == 3
    assert "calculate_sum" in tools

    await tool_manager.cleanup()
