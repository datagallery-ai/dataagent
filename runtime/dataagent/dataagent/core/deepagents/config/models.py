"""Compile legacy ``MODEL`` slots into native LangChain chat models."""

import os
from collections.abc import Mapping
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

_DEFAULT_LANGCHAIN_PROVIDER = "openai"
_OPENAI_COMPATIBLE_PROVIDERS = {
    "bailian",
    "dashscope",
    "http",
    "local",
    "openai",
    "siliconflow",
    "vllm",
    "xinference",
}
_NATIVE_LANGCHAIN_PROVIDERS = {
    "anthropic",
    "anthropic_bedrock",
    "azure_ai",
    "azure_openai",
    "baseten",
    "bedrock",
    "bedrock_converse",
    "cohere",
    "deepseek",
    "fireworks",
    "google_anthropic_vertex",
    "google_genai",
    "google_vertexai",
    "groq",
    "huggingface",
    "ibm",
    "langsmith",
    "litellm",
    "meta",
    "mistralai",
    "nvidia",
    "ollama",
    "openrouter",
    "perplexity",
    "together",
    "upstage",
    "xai",
}
_NATIVE_MODEL_PARAMS = {"max_tokens", "seed", "stop", "streaming", "temperature", "timeout", "top_p"}


class ModelConfigCompiler:
    """Compile all compatible chat models from the legacy ``MODEL`` section."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = config

    @staticmethod
    def resolve_primary_model_name(
        models: Mapping[str, BaseChatModel],
        requested_name: str | None = None,
    ) -> str:
        """Resolve the main model while preserving the legacy ``chat_model`` convention."""
        if not models:
            raise ValueError("At least one chat model is required.")
        if requested_name:
            if models.get(requested_name) is None:
                raise ValueError(f"Primary model '{requested_name}' is not available.")
            return requested_name
        if models.get("chat_model") is not None:
            return "chat_model"
        return next(iter(models))

    @staticmethod
    def resolve_langchain_provider(provider: str | None) -> str:
        """Map legacy provider names to LangChain providers, defaulting to OpenAI."""
        normalized = str(provider or "").strip().lower()
        if normalized in _OPENAI_COMPATIBLE_PROVIDERS:
            return "openai"
        if normalized in _NATIVE_LANGCHAIN_PROVIDERS:
            return normalized
        return _DEFAULT_LANGCHAIN_PROVIDER

    def compile(self) -> dict[str, BaseChatModel]:
        """Create the chat model registry and skip legacy embedding slots."""
        model_configs = self._as_mapping(self._config.get("MODEL", {}))
        models: dict[str, BaseChatModel] = {}
        for slot_name, raw_model_config in model_configs.items():
            model_config = self._as_mapping(raw_model_config)
            if str(model_config.get("model_type", "chat")).lower() == "embedding":
                continue
            models[str(slot_name)] = self._create_chat_model(str(slot_name), model_config)
        return models

    @staticmethod
    def _as_mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    def _create_chat_model(self, slot_name: str, model_config: Mapping[str, Any]) -> BaseChatModel:
        provider = str(model_config.get("provider", "")).strip().lower()
        params = dict(self._as_mapping(model_config.get("params", {})))
        model_name = str(params.pop("model", "")).strip()
        if not model_name:
            raise ValueError(f"MODEL.{slot_name}.params.model is required.")

        langchain_provider = self.resolve_langchain_provider(provider)
        env_provider = provider or langchain_provider
        base_url = params.pop("base_url", None) or params.pop("api_base", None)
        api_key = params.pop("api_key", None)
        base_url = base_url or os.getenv(f"{env_provider.upper()}_BASE_URL")
        api_key = api_key or os.getenv(f"{env_provider.upper()}_API_KEY")
        if provider in _OPENAI_COMPATIBLE_PROVIDERS - {"openai"} and not base_url:
            raise ValueError(f"MODEL.{slot_name} requires params.base_url or {env_provider.upper()}_BASE_URL.")

        params.pop("num_retries", None)
        params.pop("enable_cache_control", None)
        params.pop("disable_response_compression", None)
        extra_body = dict(self._as_mapping(params.pop("extra_body", {})))
        native_params: dict[str, Any] = {}
        for key in tuple(params):
            value = params.pop(key)
            if key in _NATIVE_MODEL_PARAMS:
                native_params[key] = value
            else:
                extra_body[key] = value
        if base_url:
            native_params["base_url"] = str(base_url)
        if api_key:
            native_params["api_key"] = str(api_key)
        if extra_body:
            native_params["extra_body"] = extra_body

        model = init_chat_model(model_name, model_provider=langchain_provider, **native_params)
        if not isinstance(model, BaseChatModel):
            raise TypeError(f"MODEL.{slot_name} did not create a concrete BaseChatModel.")
        return model
