from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dataagent.interface.sdk.agent import DataAgent


class _Config:
    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self._settings = settings or {}

    def get_all(self) -> dict[str, Any]:
        return self._settings

    def get(self, key: str, default: Any = None) -> Any:
        current: Any = self._settings
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current


def _agent(settings: dict[str, Any] | None = None) -> DataAgent:
    agent = object.__new__(DataAgent)
    agent.config = _Config(settings)
    return agent


def test_initialize_state_rejects_workspace_outside_configured_root(tmp_path: Path) -> None:
    base = tmp_path / "safe-root"
    outside = tmp_path / "outside-root"
    agent = _agent({"WORKSPACE": {"path": str(base)}})

    with pytest.raises(ValueError) as exc_info:
        agent._initialize_state(
            initial_state={"user_id": "u1", "session_id": "s1"},
            session_id="s1",
            workspace=outside,
        )

    message = str(exc_info.value)
    assert "workspace" in message
    assert str(outside.resolve()) not in message


def test_initialize_state_rejects_initial_state_workspace_outside_configured_root(tmp_path: Path) -> None:
    base = tmp_path / "safe-root"
    outside = tmp_path / "outside-root"
    agent = _agent({"WORKSPACE": {"path": str(base)}})

    with pytest.raises(ValueError):
        agent._initialize_state(
            initial_state={"user_id": "u1", "session_id": "s1", "workspace": outside},
            session_id="s1",
        )


def test_validate_workspace_file_error_does_not_include_absolute_path(tmp_path: Path) -> None:
    workspace_file = tmp_path / "not-a-dir.txt"
    workspace_file.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        DataAgent._validate_workspace(workspace_file)

    assert str(workspace_file.resolve()) not in str(exc_info.value)
