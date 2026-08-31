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
import sys

from loguru import logger

from dataagent.interface.sdk import AgentBuilder


async def test_l0_builder():
    builder = AgentBuilder()
    builder.set_base_config(
        name="Deep Analyze Agent",
        description="Deep Analyze Agent",
        agent_type="deep_analyze",
    )
    builder.set_models(
        default_chat_model={
            "name": "deepseek",
            "provider": "bailian",
            "model_type": "chat",
            "params": {"model": "deepseek-v3.2", "temperature": 0.7, "enable_thinking": True},
        },
    )
    agent = await builder.build()
    query = "What is 5 + 3 * 2"
    try:
        response = await agent.chat(query)
        assert response is not None
        logger.info(response["messages"][-1].content)
        return True
    except Exception as e:
        logger.error(f"❌ Workflow error: {e}")
        return False


if __name__ == "__main__":
    success = asyncio.run(test_l0_builder())
    sys.exit(0 if success else 1)
