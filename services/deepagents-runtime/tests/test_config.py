from deepagents_runtime.config import RuntimeSettings


def test_missing_api_key_defaults_to_fake_model():
    settings = RuntimeSettings.from_env({"LLM_MODEL": "qwen-plus"})
    assert settings.fake_model is True
    assert settings.model_configured is True


def test_api_key_defaults_to_live_model():
    settings = RuntimeSettings.from_env({"LLM_API_KEY": "sk-test", "LLM_MODEL": "qwen-plus"})
    assert settings.fake_model is False
    assert settings.llm_api_key == "sk-test"


def test_explicit_fake_overrides_api_key():
    settings = RuntimeSettings.from_env({
        "LLM_API_KEY": "sk-test",
        "DEEPAGENTS_RUNTIME_MODEL": "fake",
    })
    assert settings.fake_model is True
