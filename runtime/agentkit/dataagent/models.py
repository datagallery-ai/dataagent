"""Configured native models and process-local dependencies for ToolRuntime.context.

Agent supplies ModelContext through native invocation arguments, never through
messages, checkpoint state or client-supplied configuration. No global client cache.
"""

from _thread import LockType
from threading import Lock

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, PrivateAttr

from dataagent.bootstrap import Runtime
from dataagent.bootstrap.config_files import expand_env, validate_against
from dataagent.settings import ModelSettings


def build_model(runtime: Runtime, name: str | None = None) -> BaseChatModel:
    """Build an unbound native chat model by configured alias; never silently fall back."""
    settings = runtime.settings
    alias = settings.dataagent.model.default if name is None else name
    if alias not in settings.models:
        raise ValueError(f"Unknown model alias: {alias!r}")
    config = settings.models[alias]
    values = config.model_dump()
    values["api_key"] = config.api_key.get_secret_value()
    config = validate_against(ModelSettings, expand_env(
        values, runtime.model_environment, source=f"models.{alias}",
        home=runtime.paths.home, location=("models", alias),
    ), f"models.{alias}")
    return ChatOpenAI(
        model=config.name,
        base_url=config.base_url,
        api_key=config.api_key,
        use_responses_api=False,
        max_retries=config.max_retries,
        timeout=settings.dataagent.limits.timeout_seconds,
    )


class ModelContext(BaseModel):
    """Native model access shared by the tools in one assembled Agent graph.

    get_model() selects the product default; get_model("fast") selects models.fast.
    Clients are created lazily and reused, including concurrent sync tool calls.
    An optional model overrides only the default alias (useful for SDKs and tests).
    Dependencies are private: schema/trace serialization exposes no clients or keys.
    """

    _runtime: Runtime = PrivateAttr()
    _models: dict[str, BaseChatModel] = PrivateAttr(default_factory=dict)
    _lock: LockType = PrivateAttr(default_factory=Lock)

    def __init__(self, runtime: Runtime, *, model: BaseChatModel | None = None):
        super().__init__()
        self._runtime = runtime
        if model is not None:
            self._models[runtime.settings.dataagent.model.default] = model

    def get_model(self, name: str | None = None) -> BaseChatModel:
        """Return the configured, unbound model without changing Agent model selection."""
        alias = self._runtime.settings.dataagent.model.default if name is None else name
        with self._lock:
            if alias not in self._models:
                self._models[alias] = build_model(self._runtime, alias)
            return self._models[alias]
