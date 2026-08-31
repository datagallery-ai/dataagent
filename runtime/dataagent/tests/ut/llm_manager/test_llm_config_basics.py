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
"""UT: LLMConfig construction helpers (no live LLM calls)."""

from dataagent.core.managers.llm_manager import LLMConfig


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
