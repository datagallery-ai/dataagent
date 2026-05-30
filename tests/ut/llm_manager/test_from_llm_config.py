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
"""验证 ``LLMClient.from_llm_config`` 与 ``OpenAIProvider`` 的统一构造路径。

覆盖点：

* ``LLMClient.from_llm_config`` 正确解析 ``{PROVIDER}_BASE_URL`` /
  ``{PROVIDER}_API_KEY`` 环境变量；
* ``client_params()`` 中的 ``base_url`` / ``api_key`` 优先于环境变量；
* 未配置 ``.env`` 时，可从 ``params`` 读取 ``base_url`` / ``api_key``；
* ``client_params()`` 中的非显式字段（如 ``temperature`` / ``enable_thinking``）
  作为 litellm 透传 kwargs 落入 ``_extra``；
* 缺失模型 / URL / API key 时显式抛 ``ValueError``；
* ``OpenAIProvider.create_llm`` 返回的就是 ``LLMClient`` 实例（不再依赖
  ``langchain_community``）。
"""

from __future__ import annotations

import pytest

from dataagent.core.managers.llm_manager.llm_client import LLMClient
from dataagent.core.managers.llm_manager.llm_config import LLMConfig
from dataagent.core.managers.llm_manager.providers import OpenAIProvider


def _make_config(**params_overrides) -> LLMConfig:
    params: dict = {"model": "deepseek-v3.2", "temperature": 0.0, **params_overrides}
    return LLMConfig(
        name="qwen3_coder",
        provider="qwen3_coder",
        model_type="chat",
        params=params,
    )


def test_from_llm_config_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN3_CODER_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("QWEN3_CODER_API_KEY", "sk-test")

    client = LLMClient.from_llm_config(_make_config(enable_thinking=True))

    assert isinstance(client, LLMClient)
    assert client._model == "deepseek-v3.2"
    assert client._api_base == "https://example.invalid/v1"
    assert client._api_key == "sk-test"
    # tool_call_mode 默认 native
    assert client._tool_call_mode == "native"
    # YAML 透传字段会进 _extra（litellm kwargs）
    assert client._extra.get("enable_thinking") is True
    # 默认补齐 custom_llm_provider=openai，沿用旧 OpenAIProvider 语义
    assert client._extra.get("custom_llm_provider") == "openai"


def test_from_llm_config_params_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """params 中的 base_url / api_key 优先于 env。"""
    monkeypatch.setenv("QWEN3_CODER_BASE_URL", "https://from-env/v1")
    monkeypatch.setenv("QWEN3_CODER_API_KEY", "sk-env")

    client = LLMClient.from_llm_config(_make_config(base_url="https://from-yaml/v1", api_key="sk-yaml"))

    assert client._api_base == "https://from-yaml/v1"
    assert client._api_key == "sk-yaml"
    assert "base_url" not in client._extra
    assert "api_key" not in client._extra


def test_from_llm_config_reads_params_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置 .env 时，可从 YAML params 读取 base_url / api_key（quickstart 场景）。"""
    monkeypatch.delenv("QWEN3_CODER_BASE_URL", raising=False)
    monkeypatch.delenv("QWEN3_CODER_API_KEY", raising=False)

    client = LLMClient.from_llm_config(_make_config(base_url="https://from-yaml/v1", api_key="sk-yaml"))

    assert client._api_base == "https://from-yaml/v1"
    assert client._api_key == "sk-yaml"


def test_from_llm_config_missing_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN3_CODER_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("QWEN3_CODER_API_KEY", "sk-test")

    config = LLMConfig(
        name="qwen3_coder",
        provider="qwen3_coder",
        model_type="chat",
        params={},
    )
    with pytest.raises(ValueError, match="Missing model"):
        LLMClient.from_llm_config(config)


def test_from_llm_config_missing_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QWEN3_CODER_BASE_URL", raising=False)
    monkeypatch.setenv("QWEN3_CODER_API_KEY", "sk-test")
    with pytest.raises(ValueError, match="BASE_URL"):
        LLMClient.from_llm_config(_make_config())


def test_from_llm_config_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN3_CODER_BASE_URL", "https://example.invalid/v1")
    monkeypatch.delenv("QWEN3_CODER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        LLMClient.from_llm_config(_make_config())


def test_openai_provider_returns_llm_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAIProvider.create_llm 应统一返回 LLMClient（不再返回 ChatLiteLLM 子类）。"""
    monkeypatch.setenv("QWEN3_CODER_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("QWEN3_CODER_API_KEY", "sk-test")

    provider = OpenAIProvider()
    llm = provider.create_llm(_make_config())

    assert isinstance(llm, LLMClient)
