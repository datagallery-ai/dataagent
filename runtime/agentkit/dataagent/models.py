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
from dataagent.semantic_client import SemanticServiceClient
from dataagent.settings import ModelSettings


def build_model(runtime: Runtime, name: str | None = None) -> BaseChatModel:
    """Build an unbound native chat model by configured alias; never silently fall back."""
    settings = runtime.settings
    alias = settings.dataagent.model.default if name is None else name
    if alias not in settings.models:
        raise ValueError(f"Unknown model alias: {alias!r}")
    config = settings.models.get(alias)
    values = config.model_dump()
    values["api_key"] = config.api_key.get_secret_value()
    config = validate_against(
        ModelSettings,
        expand_env(
            values,
            runtime.model_environment,
            source=f"models.{alias}",
            home=runtime.paths.home,
            location=("models", alias),
        ),
        f"models.{alias}",
    )
    return ChatOpenAI(
        model=config.name,
        base_url=config.base_url,
        api_key=config.api_key,
        use_responses_api=False,
        max_retries=config.max_retries,
        timeout=settings.dataagent.limits.timeout_seconds,
    )


class ModelContext(BaseModel):
    """Native model and semantic access shared by one Agent graph and its subagents.

    get_model() selects the product default; get_model("fast") selects models.fast.
    Clients are created lazily and reused, including concurrent sync tool calls.
    An optional model overrides only the default alias (useful for SDKs and tests).
    Dependencies are private: schema/trace serialization exposes no clients or keys.
    The semantic client is borrowed from the SDK handle or REST host that owns it.
    """

    _runtime: Runtime = PrivateAttr()
    _models: dict[str, BaseChatModel] = PrivateAttr(default_factory=dict)
    _lock: LockType = PrivateAttr(default_factory=Lock)
    _semantic_client: SemanticServiceClient | None = PrivateAttr(default=None)

    def __init__(
        self,
        runtime: Runtime,
        *,
        model: BaseChatModel | None = None,
        semantic_client: SemanticServiceClient | None = None,
    ):
        """Retain startup model settings and an optional externally owned semantic client."""
        super().__init__()
        self._runtime = runtime
        self._semantic_client = semantic_client
        if model is not None:
            self._models[runtime.settings.dataagent.model.default] = model

    @property
    def semantic_client(self) -> SemanticServiceClient | None:
        """Return the shared semantic client without exposing it in schemas or state."""
        return self._semantic_client

    @property
    def redaction_secrets(self) -> tuple[str, ...]:
        """Include credentials from an explicitly supplied client in host diagnostics."""
        semantic = (
            self._semantic_client.redaction_secrets if self._semantic_client is not None else ()
        )
        return (*self._runtime.redaction_secrets, *semantic)

    def get_model(self, name: str | None = None) -> BaseChatModel:
        """Return the configured, unbound model without changing Agent model selection."""
        alias = self._runtime.settings.dataagent.model.default if name is None else name
        with self._lock:
            if alias not in self._models:
                self._models[alias] = build_model(self._runtime, alias)
            return self._models.get(alias)
