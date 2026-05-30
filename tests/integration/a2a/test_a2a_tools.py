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
import pytest

from dataagent.core.managers.action_manager.manager import ToolManager

tool_manager = ToolManager()


@pytest.mark.asyncio
async def test_agent_tool_register_by_sdk():
    # 注册A2A代理
    tool_manager.register_a2a_agent(agent_id="web_searcher", base_url="http://localhost:11000", timeout=120)

    # 检查代理是否已注册（全局连接层）
    agents = tool_manager.a2a_registry.list_agents()
    assert "web_searcher" in agents

    # 检查本 Agent 的注册记录
    assert "web_searcher" in tool_manager._registered_a2a_agents

    await tool_manager.cleanup()
