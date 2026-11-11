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
"""Configured native models and process-local dependencies for ToolRuntime.context.

Agent supplies ModelContext through native invocation arguments, never through
messages, checkpoint state or client-supplied configuration. No global client cache.
"""

from contextlib import AbstractContextManager
from copy import deepcopy
from pathlib import Path
from threading import Lock
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, PrivateAttr

from dataagent.bootstrap import Runtime
from dataagent.bootstrap.config_files import expand_env, validate_against
from dataagent.bootstrap.environment import selected_env_path
from dataagent.bootstrap.paths import SessionPaths
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
            dotenv=selected_env_path(runtime.paths.home, runtime.report.env_file),
        ),
        f"models.{alias}",
    )
    kwargs = {}
    if config.temperature is not None:
        kwargs["temperature"] = config.temperature
    if config.max_tokens is not None:
        kwargs["max_tokens"] = config.max_tokens
    if config.extra_body:
        kwargs["extra_body"] = dict(config.extra_body)
    return ChatOpenAI(
        model=config.name,
        base_url=config.base_url,
        api_key=config.api_key,
        use_responses_api=False,
        max_retries=config.max_retries,
        timeout=settings.dataagent.limits.timeout_seconds,
        **kwargs,
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
    _session: SessionPaths = PrivateAttr()
    _models: dict[str, BaseChatModel] = PrivateAttr(default_factory=dict)
    _lock: AbstractContextManager = PrivateAttr(default_factory=Lock)
    _semantic_client: SemanticServiceClient | None = PrivateAttr(default=None)
    _plugin_states: dict[str, dict[str, Any]] = PrivateAttr(default_factory=dict)

    def __init__(
        self,
        runtime: Runtime,
        *,
        model: BaseChatModel | None = None,
        semantic_client: SemanticServiceClient | None = None,
        session: SessionPaths | None = None,
    ):
        """Retain startup model settings and an optional externally owned semantic client."""
        super().__init__()
        self._runtime = runtime
        self._session = session or runtime.paths.for_session("sdk")
        self._semantic_client = semantic_client
        if model is not None:
            self._models[runtime.settings.dataagent.model.default] = model

    @property
    def home(self) -> Path:
        """Prepared Home for plugin-managed shared, non-session resources."""
        return self._runtime.paths.home

    @property
    def session(self) -> SessionPaths:
        """Return the immutable session paths bound to this Agent handle."""
        return self._session

    def for_session(self, session: SessionPaths) -> "ModelContext":
        """Select session paths while sharing model clients, their lock and services."""
        context = self.model_copy()
        context._session = session
        return context

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

    def get_plugin_config(self, plugin_id: str) -> dict:
        """Return isolated effective parameters for an enabled plugin."""
        if plugin_id not in self._runtime.settings.plugins.enabled:
            raise ValueError(f"Plugin {plugin_id!r} is not enabled")
        return deepcopy(self._runtime.settings.plugins.config.get(plugin_id, {}))

    def get_plugin_state(self, plugin_id: str) -> dict[str, Any]:
        """Return private, in-memory state owned by this Agent context, never checkpointed.

        Plugins synchronize their own async operations. The host lock only protects
        initial creation, and is released before any plugin work or model calls.
        """
        if plugin_id not in self._runtime.settings.plugins.enabled:
            raise ValueError(f"Plugin {plugin_id!r} is not enabled")
        with self._lock:
            return self._plugin_states.setdefault(plugin_id, {})

    def get_model(self, name: str | None = None) -> BaseChatModel:
        """Return the configured, unbound model without changing Agent model selection."""
        alias = self._runtime.settings.dataagent.model.default if name is None else name
        with self._lock:
            if alias not in self._models:
                self._models[alias] = build_model(self._runtime, alias)
            return self._models.get(alias)
