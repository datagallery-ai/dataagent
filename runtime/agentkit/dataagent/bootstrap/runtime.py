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
from dataagent.diagnostics import is_secret_name
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
            if semantic.password is not None:
                secrets.append(semantic.password.get_secret_value())
        for connection in self.extensions.mcp_servers.values():
            for section in ("env", "headers"):
                secrets.extend(value for name, value in connection.get(section, {}).items()
                               if is_secret_name(name))
            args = connection.get("args", ())
            for index, argument in enumerate(args):
                name, separator, value = argument.partition("=")
                if name.startswith("-") and is_secret_name(name):
                    if separator:
                        secrets.append(value)
                    elif index + 1 < len(args):
                        secrets.append(args[index + 1])
        for agent in self.settings.a2a.remote_agents:
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
