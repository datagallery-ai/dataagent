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
import asyncio
import time

from loguru import logger

from tests.integration.interface.test_l0_e2e_chatbi import set_chatbi_builder, test_chatbi_build


async def test_l0_e2e_async():
    """e2e：端到端异步拉起 chatbi 预制 Agent。"""
    # chatbi 预制 Agent 配置参数，其他参数保持 chatbi 预制 Agent 的默认 YAML 配置
    builder_chatbi = set_chatbi_builder()

    # 执行 chatbi 预制 Agent 的 build 流程
    total_start = time.perf_counter()
    chatbi_agent = await test_chatbi_build(builder_chatbi)
    assert chatbi_agent is not None
    logger.info("\n✅ chatbi 预制 Agent 端到端拉起完成！总耗时 {:.2f}s", time.perf_counter() - total_start)

    # 执行 chatbi 预制 Agent 的 chat 流程
    chatbi_query = "Please list all the superpowers of 3-D Man."
    chatbi_query += " 3-D Man refers to superhero_name = '3-D Man'; superpowers refers to power_name"
    chatbi_response = await chatbi_agent.chat(chatbi_query, clear_history=True)
    assert chatbi_response is not None


if __name__ == "__main__":
    asyncio.run(test_l0_e2e_async())
