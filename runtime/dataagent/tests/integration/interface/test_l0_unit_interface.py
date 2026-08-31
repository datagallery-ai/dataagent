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
import importlib

from loguru import logger

from dataagent.interface.sdk.agent import DataAgent

cli_main = importlib.import_module("dataagent.interface.cli.main")


async def test_dataAgent_from_config_chat():
    """保留 class DataAgent 老接口的测试用例，测试从 DataAgent.from_config 配置创建 Agent 并执行一次对话。"""
    logger.info("🤖 测试 class DataAgent 老接口的对话流程...")

    try:
        from dataagent.utils.runtime_paths import dataagent_package_path

        config_path = str(dataagent_package_path("core", "flex", "examples", "quickstart.yaml"))
        agent = DataAgent.from_config(config_path)
        logger.info("✅ DataAgent 老接口创建成功")

        query = "帮我查一下东风61的信息"
        logger.info(f"\n📝 用户查询: {query}")
        logger.info("-" * 60)

        response = await agent.chat(query)
        logger.info("\n🤖 Agent 响应:")
        try:
            logger.info(response["final_answer"])
        except Exception:
            logger.info(response)

        assert response is not None
    except Exception as e:
        logger.error(f"❌ DataAgent 老接口对话测试失败: {e}")
        raise


def test_run_quickstart_updates_active_model_slot(monkeypatch):
    responses = iter(["test-model", "https://example.invalid/v1", "test-key"])
    captured: dict[str, object] = {}

    class FakeDataAgent:
        def __init__(self, config):
            self.config = config
            self.name = "fake-quickstart-agent"
            captured["agent"] = self

    async def fake_chat_loop(agent, get_user_input, **kwargs):
        return None

    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    monkeypatch.setattr(cli_main, "DataAgent", FakeDataAgent)
    monkeypatch.setattr(cli_main, "_run_terminal_chat_loop", fake_chat_loop)

    asyncio.run(cli_main.run_quickstart())

    agent = captured["agent"]
    assert agent.config.get("MODEL.chat_model.params.model") == "test-model"
    assert agent.config.get("MODEL.chat_model.params.base_url") == "https://example.invalid/v1"
    assert agent.config.get("MODEL.chat_model.params.api_key") == "test-key"


def test_l0_unit_interface():
    # 老接口 asyncio.run(test_dataAgent_from_config_chat())
    logger.info("\n✅ L0 接口独立 UT 执行完成！")


if __name__ == "__main__":
    test_l0_unit_interface()
