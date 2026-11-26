"""Effective product settings: models, limits, extension selection and server configuration.

Reuses extension declarations without loading them. No file IO, environment access or
LangChain imports; the only package dependency is dataagent.config.declarations.
"""

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import (
    Field,
    JsonValue,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)

from dataagent.config.declarations import (
    NAME_PATTERN,
    OPAQUE_ID_PATTERN,
    HookSpec,
    StrictModel,
    json_containers,
    normalize_hooks,
)


class Limits(StrictModel):
    timeout_seconds: float = Field(default=180, gt=0)
    model_calls_per_agent: int = Field(default=128, gt=0)
    tool_calls_per_agent: int = Field(default=128, gt=0)


class LoopDetectionSettings(StrictModel):
    enabled: bool = Field(default=True, strict=True)
    ignored_tools: tuple[str, ...] = ()


class WorkspaceSettings(StrictModel):
    """A configured read-only input. Writable inputs are rejected in this version."""

    name: str = Field(pattern=f"^{OPAQUE_ID_PATTERN}$")
    path: Path
    read_only: Literal[True] = True


class AgentSettings(StrictModel):
    model: str = Field(
        default="primary", min_length=1
    )  # A key of models, not a provider model name.
    limits: Limits = Field(default_factory=Limits)
    loop_detection: LoopDetectionSettings = Field(default_factory=LoopDetectionSettings)
    hooks: tuple[HookSpec, ...] = ()  # Independent registrations accumulate across config layers.
    input_workspaces: tuple[WorkspaceSettings, ...] = ()
    # New sessions use <output_root>/<user>/<session>; None means <home>/outputs.
    output_root: Path | None = None
    # Compiled subagent used as the whole graph. None keeps the Deep Agents root.
    entry: str | None = Field(default=None, pattern=f"^{NAME_PATTERN}$")

    @field_validator("hooks", mode="before")
    @classmethod
    def normalize_hook_groups(cls, value):
        return normalize_hooks(value)

    @model_validator(mode="after")
    def unique_workspaces(self):
        names = [item.name for item in self.input_workspaces]
        paths = [item.path.resolve() for item in self.input_workspaces]
        if len(set(names)) != len(names):
            raise ValueError("dataagent.input_workspaces contains duplicate names")
        if len(set(paths)) != len(paths):
            raise ValueError("dataagent.input_workspaces contains duplicate paths")
        return self


class ModelSettings(StrictModel):
    provider: Literal["openai"] = "openai"
    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key: SecretStr = Field(min_length=1)
    max_retries: int = Field(default=2, ge=0, strict=True)
    # Optional call parameters; omitted fields keep the provider defaults instead of
    # being silently dropped when configuration migrates from a plugin-specific format.
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0)
    extra_body: dict[str, JsonValue] = Field(default_factory=dict)


class PluginSettings(StrictModel):
    enabled: tuple[str, ...] = ()
    paths: tuple[Path, ...] = ()
    # Per-plugin business parameters keyed by plugin ID; the plugin validates its own section.
    config: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)

    @field_validator("config", mode="before")
    @classmethod
    def plain_params(cls, value):
        """Accept frozen configuration-layer containers (tuples) as JSON values."""
        return json_containers(value)

    @model_validator(mode="after")
    def unique_enabled(self):
        if len(set(self.enabled)) != len(self.enabled):
            raise ValueError("plugins.enabled contains duplicates")
        return self


class A2AAgentSettings(StrictModel):
    """One remote agent this process can call. The tool name is ``a2a__<id>``."""

    id: str = Field(pattern=f"^{NAME_PATTERN}$")
    url: str = Field(min_length=1)
    description: str = ""
    auth_token: SecretStr | None = Field(default=None, repr=False)
    timeout_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def http_url(self):
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("a2a.remote_agents.url must start with http:// or https://")
        return self


class A2ACardSettings(StrictModel):
    """Optional overrides for the local agent's discovery metadata."""

    name: str | None = None
    description: str | None = None

    @field_validator("name", "description")
    @classmethod
    def nonblank_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class A2ASettings(StrictModel):
    """Local discovery metadata and remote agents available as tools."""

    card: A2ACardSettings = Field(default_factory=A2ACardSettings)
    remote_agents: tuple[A2AAgentSettings, ...] = ()

    @model_validator(mode="after")
    def unique_agents(self):
        names = [item.id for item in self.remote_agents]
        if len(set(names)) != len(names):
            raise ValueError("a2a.remote_agents contains duplicate ids")
        return self


class ServerSettings(StrictModel):
    """Listen address. Web and deploy read these same fields from config.yaml."""

    host: str = "127.0.0.1"
    port: int = Field(default=8790, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def bind_host(cls, value: str) -> str:
        host = value.strip()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1].strip()
        if not host or any(char.isspace() for char in host) or "/" in host:
            raise ValueError("server.host must be a bind address such as 127.0.0.1 or 0.0.0.0.")
        return host


class SemanticTLSSettings(StrictModel):
    """Explicit semantic-service TLS policy; false preserves the legacy default."""

    verify_server: bool = False
    ca_file: Path | None = None
    client_cert: Path | None = None
    client_key: Path | None = None
    ciphers: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def paired_certificate(self):
        """Require a certificate and key together for client authentication."""
        if (self.client_cert is None) != (self.client_key is None):
            raise ValueError("semantic.tls.client_cert and client_key must both be configured")
        return self


class SemanticSettings(StrictModel):
    """Connection configuration shared by semantic consumers, independent of plugins."""

    base_url: str = Field(min_length=1)
    username: str | None = Field(default=None, min_length=1, repr=False)
    password: SecretStr | None = Field(default=None, min_length=1)
    timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    semantic_scenario: str | None = Field(default=None, min_length=1)
    tls: SemanticTLSSettings = Field(default_factory=SemanticTLSSettings)

    @model_validator(mode="after")
    def paired_credentials(self):
        """Require credentials pairs, a nonblank endpoint and a sendable scenario."""
        if not self.base_url.strip():
            raise ValueError("semantic.base_url must not be blank")
        if self.semantic_scenario is not None:
            scenario = self.semantic_scenario.strip()
            if not scenario:
                raise ValueError("semantic.semantic_scenario must not be blank")
            if not (scenario.isascii() and scenario.isprintable()):
                raise ValueError("semantic.semantic_scenario must be printable ASCII")
        if (self.username is None) != (self.password is None):
            raise ValueError("semantic.username and password must both be configured")
        return self


class Settings(StrictModel):
    schema_version: Literal[4] = 4
    dataagent: AgentSettings = Field(default_factory=AgentSettings)
    models: Mapping[str, ModelSettings]
    plugins: PluginSettings = Field(default_factory=PluginSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    semantic: SemanticSettings | None = None
    a2a: A2ASettings = Field(default_factory=A2ASettings)

    @field_serializer("models", mode="wrap")
    def serialize_models(self, value, handler):
        return handler(dict(value))

    @model_validator(mode="after")
    def selected_model_exists(self):
        if self.dataagent.model not in self.models:
            raise ValueError("dataagent.model does not name a configured model")
        object.__setattr__(self, "models", MappingProxyType(dict(self.models)))
        return self


SECTION_MODELS = frozenset(
    {
        AgentSettings,
        Limits,
        LoopDetectionSettings,
        PluginSettings,
        ServerSettings,
        SemanticSettings,
        SemanticTLSSettings,
        A2ASettings,
        A2ACardSettings,
    }
)
"""Nested sections that configuration layers may declare partially."""
