"""Prepared startup results shared by Agent assembly and hosts; no live execution state.

Runtime groups effective settings, filesystem paths, extension locations and a startup
report. Each has one purpose; model display and host policy values derive from settings.
"""

from dataclasses import dataclass, field
from pathlib import Path

from dataagent.bootstrap.config_files import ConfigSource
from dataagent.bootstrap.discovery import ExtensionLocations
from dataagent.bootstrap.paths import RuntimePaths
from dataagent.settings import Settings


@dataclass(frozen=True)
class Runtime:
    """The output of `prepare_runtime`, not LangGraph Runtime or checkpointed Agent state.

    settings: what to configure; paths: where runtime data lives; extensions: where
    to load declarations; report: which configuration and plugin sources participated.
    """

    settings: Settings
    paths: RuntimePaths
    report: "StartupReport"
    extensions: ExtensionLocations = field(default_factory=ExtensionLocations)

    @property
    def model_name(self) -> str:
        return self.settings.models[self.settings.dataagent.model.default].name

    @property
    def redaction_secrets(self) -> tuple[str, ...]:
        model = self.settings.models[self.settings.dataagent.model.default]
        secrets = [model.api_key.get_secret_value()]
        for connection in self.extensions.mcp_servers.values():
            secrets.extend(connection.get("env", {}).values())
            secrets.extend(connection.get("headers", {}).values())
            # Endpoints/arguments may embed credentials; never echo them in diagnostics.
            secrets.extend(connection.get("args", ()))
            if connection.get("url"):
                secrets.append(connection["url"])
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
