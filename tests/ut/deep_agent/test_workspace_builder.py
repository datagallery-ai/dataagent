# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""YAML-to-OpenJiuWen Workspace adapter tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from dataagent.config import ConfigManager
from dataagent.core.deep_agent.adapter import DeepAgentAdapter
from dataagent.core.deep_agent.builders.workspace import build_workspace
from dataagent.interface.sdk.agent import DataAgent


def test_builds_explicit_jiuwen_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"

    workspace = build_workspace(root, language="en")

    assert Path(workspace.root_path) == root
    assert workspace.language == "en"
    assert root.is_dir()
    assert workspace.get_node_path("skills") == root / "skills"


def test_rejects_workspace_path_that_is_a_file(tmp_path: Path) -> None:
    file_path = tmp_path / "not-a-directory"
    file_path.write_text("content", encoding="utf-8")

    with pytest.raises(ValueError, match="must be a directory"):
        build_workspace(file_path)


def test_config_accepts_single_absolute_workspace_path(tmp_path: Path) -> None:
    ConfigManager._validate_workspace_yaml_config(
        {"WORKSPACE": {"path": str(tmp_path)}}
    )


def test_config_rejects_multiple_workspace_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="multiple workspace roots are not supported"):
        ConfigManager._validate_workspace_yaml_config(
            {"WORKSPACE": {"path": [str(tmp_path), str(tmp_path / "other")]}}
        )


def test_dataagent_resolves_configured_single_workspace_path(tmp_path: Path) -> None:
    agent = DataAgent({"WORKSPACE": {"path": str(tmp_path)}})

    assert agent._resolve_workspace() == tmp_path


def test_dataagent_rejects_multiple_workspace_paths(tmp_path: Path) -> None:
    agent = DataAgent({"WORKSPACE": {"path": [str(tmp_path)]}})

    with pytest.raises(ValueError, match="multiple workspace roots are not supported"):
        agent._resolve_workspace()


def test_adapter_delegates_workspace_construction(tmp_path: Path) -> None:
    workspace = DeepAgentAdapter({}).build_workspace(tmp_path)

    assert Path(workspace.root_path) == tmp_path
