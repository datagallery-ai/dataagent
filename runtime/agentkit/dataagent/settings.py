"""Effective product settings: models, limits, extension selection and server configuration.

Reuses extension declarations without loading them. No file IO, environment access or
LangChain imports; the only package dependency is dataagent.declarations.
"""

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import Field, SecretStr, field_serializer, field_validator, model_validator

from dataagent.declarations import HookSpec, StrictModel, normalize_hooks


class ModelSelection(StrictModel):
    default: str = "primary"


class Limits(StrictModel):
    timeout_seconds: float = Field(default=180, gt=0)
    model_calls_per_agent: int = Field(default=128, gt=0)
    tool_calls_per_agent: int = Field(default=128, gt=0)


class WorkspaceSettings(StrictModel):
    """A configured read-only input. Writable inputs are rejected in this version."""

    name: str = Field(pattern=r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
    path: Path
    read_only: Literal[True] = True


class AgentSettings(StrictModel):
    model: ModelSelection = Field(default_factory=ModelSelection)
    limits: Limits = Field(default_factory=Limits)
    hooks: tuple[HookSpec, ...] = ()  # Independent registrations accumulate across config layers.
    workspaces: tuple[WorkspaceSettings, ...] = ()

    @field_validator("hooks", mode="before")
    @classmethod
    def normalize_hook_groups(cls, value):
        return normalize_hooks(value)

    @model_validator(mode="after")
    def unique_workspaces(self):
        names = [item.name for item in self.workspaces]
        paths = [item.path.resolve() for item in self.workspaces]
        if len(set(names)) != len(names):
            raise ValueError("dataagent.workspaces contains duplicate names")
        if len(set(paths)) != len(paths):
            raise ValueError("dataagent.workspaces contains duplicate paths")
        return self


class ModelSettings(StrictModel):
    provider: Literal["openai"] = "openai"
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: SecretStr = Field(min_length=1)
    max_retries: int = Field(default=2, ge=0, strict=True)


class PluginSettings(StrictModel):
    enabled: tuple[str, ...] = ()
    paths: tuple[Path, ...] = ()

    @model_validator(mode="after")
    def unique_enabled(self):
        if len(set(self.enabled)) != len(self.enabled):
            raise ValueError("plugins.enabled contains duplicates")
        return self


class ServerSettings(StrictModel):
    """The only section owned by the REST host; the core validates it but never reads it."""

    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8790, ge=1, le=65535)


class Settings(StrictModel):
    schema_version: Literal[3] = 3
    dataagent: AgentSettings = Field(default_factory=AgentSettings)
    models: Mapping[str, ModelSettings]
    plugins: PluginSettings = Field(default_factory=PluginSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)

    @field_serializer("models", mode="wrap")
    def serialize_models(self, value, handler):
        return handler(dict(value))

    @model_validator(mode="after")
    def selected_model_exists(self):
        if self.dataagent.model.default not in self.models:
            raise ValueError("dataagent.model.default does not name a configured model")
        object.__setattr__(self, "models", MappingProxyType(dict(self.models)))
        return self


SECTION_MODELS = frozenset({AgentSettings, ModelSelection, Limits, PluginSettings, ServerSettings})
"""Nested sections that configuration layers may declare partially."""
