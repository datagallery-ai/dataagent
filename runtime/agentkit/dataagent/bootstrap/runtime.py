"""Prepared startup results shared by Agent assembly and hosts; no live execution state.

PreparedRuntime groups effective settings, filesystem paths, extension locations and a startup
report. Each has one purpose; model display and host policy values derive from settings.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dataagent.bootstrap.config_files import ENV_REFERENCE, ConfigSource
from dataagent.bootstrap.discovery import ExtensionLocations
from dataagent.bootstrap.paths import RuntimePaths
from dataagent.settings import Settings


@dataclass(frozen=True)
class PreparedRuntime:
    """The output of `prepare_runtime`, not LangGraph PreparedRuntime or checkpointed Agent state.

    settings: what to configure; paths: where runtime data lives; extensions: where
    to load declarations; report: which configuration and plugin sources participated;
    model_environment: startup values referenced by lazily used models (no live clients).
    a2a_auth_token: DATAAGENT_A2A_AUTH_TOKEN from the effective environment, or None.
    """

    settings: Settings
    paths: RuntimePaths
    report: "StartupReport"
    extensions: ExtensionLocations = field(default_factory=ExtensionLocations)
    model_environment: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)
    a2a_auth_token: str | None = field(default=None, repr=False, compare=False)

    @property
    def model_name(self) -> str:
        return self.settings.models[self.settings.dataagent.model].name

    @property
    def redaction_secrets(self) -> tuple[str, ...]:
        # Include secondary keys without requiring unused models to be configured.
        # Model names/URLs are diagnostic data, not automatically treated as secrets.
        secrets = [
            ENV_REFERENCE.sub(
                lambda match: self.model_environment.get(match[1], match[0]),
                model.api_key.get_secret_value(),
            )
            for model in self.settings.models.values()
        ]
        semantic = self.settings.semantic
        if semantic is not None:
            if semantic.username:
                secrets.append(semantic.username)
            if semantic.password is not None:
                secrets.append(semantic.password.get_secret_value())
            secrets.append(semantic.base_url)
        for connection in self.extensions.mcp_servers.values():
            secrets.extend(connection.get("env", {}).values())
            secrets.extend(connection.get("headers", {}).values())
            # Endpoints/arguments may embed credentials; never echo them in diagnostics.
            secrets.extend(connection.get("args", ()))
            if connection.get("url"):
                secrets.append(connection["url"])
        for agent in self.settings.a2a.remote_agents:
            secrets.append(str(agent.url))
            if agent.auth_token is not None:
                secrets.append(agent.auth_token.get_secret_value())
        if self.a2a_auth_token:
            secrets.append(self.a2a_auth_token)
        return tuple(dict.fromkeys(value for value in secrets if value))

    @property
    def timeout_seconds(self) -> float:
        return self.settings.dataagent.limits.timeout_seconds


@dataclass(frozen=True)
class StartupReport:
    """Configuration participation and enabled plugins' candidate origins; no config values."""

    configs: tuple[ConfigSource, ...]
    plugin_origins: tuple[tuple[str, tuple[str, ...]], ...]
    env_file: Path | None = None
