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


def test_create_llm():
    """测试LLM管理器（会触发真实 LLM 调用，需在具备密钥的集成环境运行）"""
    config = {
        "name": "DEEPSEEK_CHAT",
        "model_type": "chat",
        "provider": "openai",
        "params": {
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "api_key": os.getenv("DEEPSEEK_CHAT_API_KEY", ""),
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
