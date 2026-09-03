"""Tests for native ConfigManager reload and Suite expansion."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from dataagent.config import ConfigManager


def _write_yaml(path: Path, payload: dict) -> Path:
    """Write a YAML mapping and return its path."""
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_reload_interpolates_current_mapping(tmp_path: Path) -> None:
    """Interpolation resolves against the mapping being loaded."""
    config_path = _write_yaml(
        tmp_path / "agent.yaml",
        {
            "MODEL": {"primary": {"params": {"model": "deepseek-chat"}}},
            "REFERENCE": "${MODEL.primary.params.model}",
        },
    )
    config = ConfigManager(config_path)
    assert config.get("REFERENCE") == "deepseek-chat"


def test_reload_failure_preserves_committed_settings(tmp_path: Path) -> None:
    """A validation failure leaves the last successful configuration intact."""
    good_path = _write_yaml(tmp_path / "good.yaml", {"AGENT_CONFIG": {"name": "good"}})
    bad_path = _write_yaml(tmp_path / "bad.yaml", {"WORKSPACE": {"path": "relative/not-allowed"}})
    config = ConfigManager(good_path)
    previous = copy.deepcopy(config.settings)

    with pytest.raises(ValueError, match="absolute path"):
        config.reload(str(bad_path))

    assert config.settings == previous


def test_reload_expands_suite_before_compilation(tmp_path: Path) -> None:
    """Suite metadata stays at the YAML boundary and does not reach the compiler mapping."""
    config_path = _write_yaml(tmp_path / "suite.yaml", {"SUITE": {"include": ["data_analysis"]}})
    config = ConfigManager(config_path)

    assert config.get("SUITE") is None
    assert config.activated_suites[0].get("name") == "data_analysis"
    assert config.get("SUBAGENTS")
