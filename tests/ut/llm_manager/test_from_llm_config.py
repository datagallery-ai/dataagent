from __future__ import annotations

import pytest
from dataagent.core.managers.llm_manager.adapters import LangChainChatModelAdapter
from dataagent.core.managers.llm_manager.llm_client import LLMClient
from dataagent.core.managers.llm_manager.llm_config import LLMConfig
from dataagent.core.managers.llm_manager.llm_manager import LLMManager


def _make_config(**params_overrides) -> LLMConfig:
    params: dict = {"model": "deepseek-v3.2", "temperature": 0.0, **params_overrides}
    return LLMConfig(
        name="qwen3_coder",
        provider="qwen3_coder",
        model_type="chat",
        params=params,
    )


def test_llm_config_string_representations_do_not_expose_client_params() -> None:
    """repr/str must only expose safe LLM metadata."""
    config = _make_config(
        api_key="sk-sensitive-secret",
        base_url="https://user:password@internal.example/v1",
        headers={"Authorization": "Bearer private-token"},
    )

    expected = "LLMConfig(name='qwen3_coder', provider='qwen3_coder', model_type='chat', section='qwen3_coder')"
    assert repr(config) == expected
    assert str(config) == expected
    assert "sk-sensitive-secret" not in repr(config)
    assert "private-token" not in repr(config)
    assert "internal.example" not in repr(config)

    assert config.client_params()["api_key"] == "sk-sensitive-secret"
    assert config.to_dict()["api_key"] == "sk-sensitive-secret"


def test_from_llm_config_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN3_CODER_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("QWEN3_CODER_API_KEY", "sk-test")

    client = LLMClient.from_llm_config(_make_config(enable_thinking=True))

    assert isinstance(client, LLMClient)
    assert client._model == "deepseek-v3.2"
    assert client._api_base == "https://example.invalid/v1"
    assert client._api_key == "sk-test"
    assert client._extra.get("enable_thinking") is True
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


def test_llm_manager_create_llm_uses_llm_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLMManager.create_llm 底层应为 LLMClient（经 LangChainChatModelAdapter 包装）。"""
    monkeypatch.setenv("QWEN3_CODER_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("QWEN3_CODER_API_KEY", "sk-test")

    manager = LLMManager()
    adapter = manager.create_llm(_make_config())

    assert isinstance(adapter, LangChainChatModelAdapter)
    assert isinstance(adapter.raw, LLMClient)


def test_llm_manager_create_llm_embedding_registers_config_only() -> None:
    """embedding 模型只缓存配置，不构造 LLMClient。"""
    manager = LLMManager()
    config = LLMConfig(
        name="jina_v3",
        provider="jina",
        model_type="embedding",
        params={"model": "jina-embeddings-v3"},
    )

    instance = manager.create_llm(config)

    assert instance is None
    assert manager.get_llm("jina_v3") is None
    assert manager.get_llm_config("jina_v3") is config
