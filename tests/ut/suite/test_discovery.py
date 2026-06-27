# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Tests for Suite discovery paths and reload gating."""

from pathlib import Path

import pytest
import yaml

from dataagent.config.config_manager import ConfigManager
from dataagent.suite.discovery import discover_suite_index, scan_suite_paths
from dataagent.utils.runtime_paths import dataagent_home, dataagent_package_path


def test_scan_suite_paths_user_and_builtin_suites() -> None:
    paths = scan_suite_paths()
    assert paths[0] == dataagent_home() / "suites"
    builtin_suites = dataagent_package_path("suite", "builtin_suites")
    assert builtin_suites in paths
    assert paths[-1] == builtin_suites
    assert dataagent_package_path("suites") not in paths
    assert not any(".dataagent" in str(p) and "suites" in str(p) for p in paths if p != paths[0])


def test_reload_without_suite_skips_discovery(monkeypatch) -> None:
    called = {"discover": False}

    def _fake_discover(**_kwargs):
        called["discover"] = True
        return {}

    monkeypatch.setattr("dataagent.suite.discovery.discover_suite_index", _fake_discover)
    default = dataagent_package_path("examples", "default_configs.yaml")
    cm = ConfigManager()
    cm.reload(
        str(dataagent_package_path("examples", "quickstart.yaml")),
        str(default),
    )
    assert called["discover"] is False
    assert cm.activated_suites == []


def _install_invalid_hooks_suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, hook_spec: str) -> Path:
    """Create a Suite whose hooks.yaml contains an invalid hook entry."""
    home = tmp_path / "dataagent_home"
    root = home / "suites" / "bad_hooks_suite"
    root.mkdir(parents=True)
    (root / "suite.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "bad_hooks_suite",
                "enabled": True,
                "priority": 0,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    hooks_dir = root / "hooks"
    hooks_dir.mkdir()
    hooks_doc = {"HOOKS": {"nodes": {"planner": {"pre": [hook_spec]}}}}
    (hooks_dir / "hooks.yaml").write_text(yaml.safe_dump(hooks_doc), encoding="utf-8")
    monkeypatch.setenv("DATAAGENT_HOME", str(home))
    return root


def test_discover_skips_suite_with_builtin_hook_short_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scan must reject Suite hooks.yaml entries that use builtin short names."""
    _install_invalid_hooks_suite(tmp_path, monkeypatch, hook_spec="pruner")
    index = discover_suite_index()
    assert "bad_hooks_suite" not in index


def test_discover_accepts_suite_with_framework_hook_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scan must accept Suite hooks.yaml entries that reference framework ``dataagent.*`` paths."""
    home = tmp_path / "dataagent_home"
    root = home / "suites" / "framework_hook_suite"
    hooks_dir = root / "hooks"
    hooks_dir.mkdir(parents=True)
    (root / "suite.yaml").write_text(
        yaml.safe_dump({"name": "framework_hook_suite", "enabled": True}, sort_keys=False),
        encoding="utf-8",
    )
    hooks_doc = {
        "HOOKS": {
            "agent": {
                "post": ["dataagent.core.flex.hooks.organize_workspace.organize_workspace"],
            }
        }
    }
    (hooks_dir / "hooks.yaml").write_text(yaml.safe_dump(hooks_doc), encoding="utf-8")
    monkeypatch.setenv("DATAAGENT_HOME", str(home))
    index = discover_suite_index()
    assert "framework_hook_suite" in index


def test_discover_skips_suite_with_invalid_dotted_hook_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scan must reject hook specs that are not module.path.callable."""
    _install_invalid_hooks_suite(tmp_path, monkeypatch, hook_spec="hooks.bad")
    index = discover_suite_index()
    assert "bad_hooks_suite" not in index


def test_discover_skips_suite_with_empty_hook_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scan must reject empty hook spec strings."""
    _install_invalid_hooks_suite(tmp_path, monkeypatch, hook_spec="")
    index = discover_suite_index()
    assert "bad_hooks_suite" not in index


def test_discover_skips_suite_with_invalid_enabled_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scan must reject suite.yaml when enabled is a non-boolean string."""
    home = tmp_path / "dataagent_home"
    root = home / "suites" / "bad_enabled_suite"
    root.mkdir(parents=True)
    (root / "suite.yaml").write_text(
        yaml.safe_dump({"name": "bad_enabled_suite", "enabled": "false"}, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATAAGENT_HOME", str(home))
    index = discover_suite_index()
    assert "bad_enabled_suite" not in index


def test_discover_skips_suite_with_invalid_priority_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scan must reject suite.yaml when priority is bool instead of int."""
    home = tmp_path / "dataagent_home"
    root = home / "suites" / "bad_priority_suite"
    root.mkdir(parents=True)
    (root / "suite.yaml").write_text(
        yaml.safe_dump({"name": "bad_priority_suite", "priority": True}, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATAAGENT_HOME", str(home))
    index = discover_suite_index()
    assert "bad_priority_suite" not in index


def test_discover_skips_suite_with_empty_hook_dict_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scan must reject hooks.yaml dict entries with empty name."""
    home = tmp_path / "dataagent_home"
    root = home / "suites" / "bad_hook_name_suite"
    root.mkdir(parents=True)
    (root / "suite.yaml").write_text(
        yaml.safe_dump({"name": "bad_hook_name_suite", "enabled": True}, sort_keys=False),
        encoding="utf-8",
    )
    hooks_dir = root / "hooks"
    hooks_dir.mkdir()
    hooks_doc = {"HOOKS": {"nodes": {"planner": {"pre": [{"name": ""}]}}}}
    (hooks_dir / "hooks.yaml").write_text(yaml.safe_dump(hooks_doc), encoding="utf-8")
    monkeypatch.setenv("DATAAGENT_HOME", str(home))
    index = discover_suite_index()
    assert "bad_hook_name_suite" not in index


def test_discover_skips_suite_with_prefixed_hook_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scan must reject Suite hooks.yaml entries that already include the suite name prefix."""
    _install_invalid_hooks_suite(
        tmp_path,
        monkeypatch,
        hook_spec="bad_hooks_suite.hooks.custom_hooks.hook",
    )
    index = discover_suite_index()
    assert "bad_hooks_suite" not in index
