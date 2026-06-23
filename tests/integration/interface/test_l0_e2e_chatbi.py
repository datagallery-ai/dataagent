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
from pathlib import Path
from typing import Any

import yaml
from dataagent.interface.sdk.builder import AgentBuilder
from dataagent.utils.builder_utils import resolve_example_yaml_path
from loguru import logger


def _strip_chatbi_sensitive_fields(data: Any) -> Any:
    if isinstance(data, dict):
        return {
            key: ("__SENSITIVE__" if key in {"base_url", "api_key"} else _strip_chatbi_sensitive_fields(value))
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [_strip_chatbi_sensitive_fields(item) for item in data]
    return data


def _assert_chatbi_sensitive_fields_masked(data: Any) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            if key in {"base_url", "api_key"}:
                assert isinstance(value, str)
                assert value
                assert set(value) == {"*"}
            else:
                _assert_chatbi_sensitive_fields_masked(value)
        return
    if isinstance(data, list):
        for item in data:
            _assert_chatbi_sensitive_fields_masked(item)


def test_chatbi_set_base_config(builder):
    """验证 chatbi 基础配置可正确写入全局配置。"""
    builder.set_base_config(
        name="NL2SQL Agent",
        description="智能问数Agent",
        agent_type="chatbi",
        chat_model={
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key": "sk-9dcec001c21c4b0da5e1ac5ec0091c84",
        },
    )

    assert builder._global_config["AGENT_CONFIG"]["agent_type"] == "chatbi"


def test_chatbi_set_models(builder):
    """验证 chatbi 的默认模型配置可正确写入。"""
    builder.set_models(
        default_chat_model={
            "model_type": "chat",
            "provider": "bailian",
            "params": {
                "model": "qwen-plus",
                "temperature": 0.1,
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "api_key": "sk-9dcec001c21c4b0da5e1ac5ec0091c84",
            },
        },
        default_embedding_model={
            "model_type": "embedding",
            "provider": "deepseek",
            "params": {
                "model": "text-embedding-v1",
                "base_url": "https://api.deepseek.com",
                "api_key": "sk-1234567890",
            },
        },
    )

    assert "MODEL" in builder._global_config


def test_chatbi_set_scenario(builder):
    """验证 chatbi 的场景注入配置可正确写入。"""
    instructions = "请优先基于 schema 进行字段语义匹配。"
    constraint = "SQL 结果需可解释，且给出关键过滤条件。"
    builder.set_scenario(
        instructions=instructions,
        constraint=constraint,
    )

    scenario_chat = builder._global_config["SCENARIO"]["chat"]
    assert scenario_chat["instructions"] == instructions
    assert scenario_chat["constraints"] == constraint


async def test_chatbi_build(builder):
    """验证 chatbi 预制 Agent 构建流程及关键调用链。"""
    total_start = time.perf_counter()

    # 调用 AgentBuilder 的 build 方法（L0 接口）
    result = await builder.build()
    await asyncio.sleep(2.0)

    # 关键断言
    output_dir = Path(__file__).resolve().parents[3] / "dataagent/interface/sdk/output"
    original_yaml_path = output_dir / "original_chatbi_config.yaml"
    temp_yaml_path = output_dir / "temp_chatbi_config.yaml"
    merged_yaml_path = output_dir / "merged_chatbi_config.yaml"
    example_yaml_path = resolve_example_yaml_path(agent_type="chatbi")
    assert example_yaml_path is not None
    assert original_yaml_path.exists()
    assert temp_yaml_path.exists()
    assert merged_yaml_path.exists()
    example_yaml_data = yaml.safe_load(example_yaml_path.read_text(encoding="utf-8")) or {}
    merged_yaml_data = yaml.safe_load(merged_yaml_path.read_text(encoding="utf-8")) or {}
    # 验证output 目录下 YAML 文件内容脱敏后覆盖写回
    assert _strip_chatbi_sensitive_fields(example_yaml_data) == _strip_chatbi_sensitive_fields(merged_yaml_data)
    # 验证original/example YAML 文件 SCENARIO 内容增量追加
    merged_scenario_chat = (merged_yaml_data.get("SCENARIO") or {}).get("chat") or {}
    assert "请优先基于 schema 进行字段语义匹配。" in str(merged_scenario_chat.get("instructions", ""))
    assert "SQL 结果需可解释，且给出关键过滤条件。" in str(merged_scenario_chat.get("constraints", ""))
    _assert_chatbi_sensitive_fields_masked(merged_yaml_data)
    assert result is not None

    logger.info(
        "\n✅ chatbi 预制 Agent 端到端拉起完成，总耗时 {:.2f}s，example yaml path: {}，merged yaml path: {}",
        time.perf_counter() - total_start,
        example_yaml_path,
        merged_yaml_path,
    )
    return result


def set_chatbi_builder():
    """构建 chatbi 预制 Agent builder"""
    builder = AgentBuilder()
    test_chatbi_set_base_config(builder)
    test_chatbi_set_models(builder)
    test_chatbi_set_scenario(builder)
    # chatbi 场景实际加载的是 set 相关配置后的 dataagent/agents/nl2sql/nl2sql_agent.yaml
    return builder


async def test_l0_e2e_chatbi():
    """e2e：端到端拉起 chatbi 预制 Agent。"""
    # 设置 chatbi 预制 Agent 配置参数，调用 set 相关方法
    builder = set_chatbi_builder()
    # 构建 chatbi 预制 Agent，调用 build 方法
    agent = await test_chatbi_build(builder)
    # 执行对话，调用的是 DataAgent 的 chat 方法（L0 接口），会通过 select_engine 方法选择 NL2SQLAgent 的 chat 方法
    query = "Please list all the superpowers of 3-D Man."
    query += " 3-D Man refers to superhero_name = '3-D Man'; superpowers refers to power_name"
    response = await agent.chat(query)
    # 真实执行时，至少应返回可用响应对象
    assert response is not None


if __name__ == "__main__":
    asyncio.run(test_l0_e2e_chatbi())
