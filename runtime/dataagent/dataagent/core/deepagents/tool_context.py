"""Per-call context injected into YAML-defined Python tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dataagent.config.config_manager import ConfigManager


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Carry native tool runtime data without exposing it in the model-visible schema.

    ``runtime`` is normally LangChain's ``ToolRuntime``. Legacy pending-migration
    tools may still receive their historical runtime object through the same field.
    ``job_envelope`` remains available while the jobs/resource features are pending
    migration.
    """

    config_manager: ConfigManager | None = None
    tool_config: dict[str, Any] = field(default_factory=dict)
    runtime: Any | None = None
    job_envelope: dict[str, Any] = field(default_factory=dict)
