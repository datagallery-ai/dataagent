# Licensed under the Apache License, Version 2.0 (the "License");
"""Analysis data scope for physical and inline subagent sessions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class AnalysisScope:
    """Select artifacts belonging to one logical session inside a workspace."""

    kind: str = "session"
    session_id: str = ""
    parent_session_id: str = ""
    sub_id: str = ""
    context_files: tuple[str, ...] = ()
    performance_files: tuple[str, ...] = ()
    log_files: tuple[str, ...] = ()
    performance_time_window: Optional[tuple[float, float]] = None
    performance_match_mode: str = ""
    performance_error: str = ""

    @property
    def is_inline(self) -> bool:
        """Return whether the logical session shares its parent's workspace."""
        return self.kind == "inline_shared_workspace"

    @property
    def is_shared_workspace(self) -> bool:
        """Return whether artifact selection must not fall back to every workspace file."""
        return self.kind in {"inline_shared_workspace", "main_shared_workspace"}

    @staticmethod
    def _existing_files(values: tuple[str, ...]) -> list[Path]:
        return sorted({Path(value).expanduser().resolve() for value in values if Path(value).expanduser().is_file()})

    @classmethod
    def from_value(cls, value: Any) -> Optional[AnalysisScope]:
        """Normalize a serialized scope dictionary or an existing scope object."""
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return None
        time_window = value.get("performance_time_window")
        normalized_window = None
        if isinstance(time_window, (list, tuple)) and len(time_window) == 2:
            normalized_window = (float(time_window[0]), float(time_window[1]))
        return cls(
            kind=str(value.get("kind", "session")),
            session_id=str(value.get("session_id", "")),
            parent_session_id=str(value.get("parent_session_id", "")),
            sub_id=str(value.get("sub_id", "")),
            context_files=tuple(str(path) for path in value.get("context_files", [])),
            performance_files=tuple(str(path) for path in value.get("performance_files", [])),
            log_files=tuple(str(path) for path in value.get("log_files", [])),
            performance_time_window=normalized_window,
            performance_match_mode=str(value.get("performance_match_mode", "")),
            performance_error=str(value.get("performance_error", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a pickle- and JSON-friendly representation."""
        return asdict(self)

    def resolve_context_files(self, session_root: Path) -> list[Path]:
        """Return selected context files, or all run files for an unscoped session."""
        if self.context_files:
            return self._existing_files(self.context_files)
        if self.is_shared_workspace:
            return []
        return sorted(
            path for path in (session_root / ".context").glob("Run*.json") if not path.name.endswith(".meta.json")
        )

    def resolve_performance_files(self, session_root: Path) -> list[Path]:
        """Return selected performance files, or all JSONL files for an unscoped session."""
        if self.performance_files:
            return self._existing_files(self.performance_files)
        if self.is_shared_workspace:
            return []
        return sorted((session_root / ".performance").glob("*.jsonl"))

    def resolve_log_files(self) -> list[Path]:
        """Return explicitly selected log files."""
        return self._existing_files(self.log_files)
