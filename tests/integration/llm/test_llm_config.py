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
from dataagent.core.managers.llm_manager import LLMConfig, llm_manager


def test_llm_config_creation():
    """测试LLM配置创建"""
    config = LLMConfig(
        name="gpt-4",
        provider="openai",
        model_type="chat",
        base_url="https://api.openai.com/v1",
        temperature=0.7,
        max_tokens=2048,
    )

    assert config.name == "gpt-4"
    assert config.provider == "openai"
    assert config.model_type == "chat"
    assert config.client_kwargs["base_url"] == "https://api.openai.com/v1"
    assert config.client_kwargs["temperature"] == 0.7
    assert config.client_kwargs["max_tokens"] == 2048


def test_llm_config_from_dict():
    """测试从字典创建LLM配置"""
    config_dict = {
        "name": "gpt-3.5-turbo",
        "model_type": "chat",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "temperature": 0.5,
    }

    config = LLMConfig.from_dict(config_dict)
    assert config.name == "gpt-3.5-turbo"
    assert config.model_type == "chat"
    assert config.client_kwargs["base_url"] == "https://api.openai.com/v1"
    assert config.client_kwargs["temperature"] == 0.5


def test_create_llm(monkeypatch: pytest.MonkeyPatch):
    """测试 LLM 管理器（真实调用；须设置 DEEPSEEK_CHAT_BASE_URL / DEEPSEEK_CHAT_API_KEY）。

    ``LLMClient.from_llm_config`` 只从 ``{PROVIDER}_BASE_URL`` / ``{PROVIDER}_API_KEY`` 读连接信息，
    ``params.base_url`` / ``params.api_key`` 会被忽略，因此 ``provider`` 须与 env 前缀一致。
    """
    api_key = os.getenv("DEEPSEEK_CHAT_API_KEY")
    if not api_key:
        pytest.skip("DEEPSEEK_CHAT_API_KEY not set")
    base_url = os.getenv("DEEPSEEK_CHAT_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("DEEPSEEK_CHAT_BASE_URL", base_url)
    monkeypatch.setenv("DEEPSEEK_CHAT_API_KEY", api_key)

    config = {
        "name": "DEEPSEEK_CHAT",
        "model_type": "chat",
        "provider": "deepseek_chat",
        "params": {
            "model": "deepseek-chat",
            "temperature": 0.7,
            "max_tokens": 2048,
            "timeout": 90,
            "max_retries": 3,
        },
    }

    # 注册LLM配置
    config = LLMConfig.from_dict(config)
    llm_manager.create_llm(config)

    # 测试列表
    assert "DEEPSEEK_CHAT" in llm_manager.list_llms()

    # 获取llm
    llm = llm_manager.get_llm("DEEPSEEK_CHAT")
    result = llm.invoke("Hello, how are you?")
    assert result.content is not None

    deepseek_dict = {
        "name": "deepseek",
        "provider": "openai",
        "model_type": "chat",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
    }

    # 实例化LLMConfig
    LLMConfig(
        name="deepseek",
        provider="openai",
        model_type="chat",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
    )

    # 利用词典实例化LLMConfig
    deepseek_config1 = LLMConfig.from_dict(deepseek_dict)

    # 获取参数词典
    assert deepseek_config1.client_params() == deepseek_dict
