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
"""Tests for activated Suite root resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dataagent.config.config_manager import ConfigManager
from dataagent.suite.activated_suites import resolve_activated_suite_root
from dataagent.utils.runtime_paths import dataagent_package_path

DEFAULT_CONFIG = dataagent_package_path("examples", "default_configs.yaml")


def test_resolve_activated_suite_root_returns_absolute_path() -> None:
    """Activated Suite metadata must resolve to an absolute root path."""
    suite_root = dataagent_package_path("suite", "builtin_suites", "example_suite")
    activated = [{"name": "example_suite", "root": str(suite_root)}]
    resolved = resolve_activated_suite_root("example_suite", activated)
    assert resolved.is_absolute()
    assert resolved == suite_root.resolve()


def test_resolve_activated_suite_root_rejects_empty_name() -> None:
    """Empty suite names must be rejected."""
    with pytest.raises(ValueError, match="suite_name must be non-empty"):
        resolve_activated_suite_root("", [{"name": "example_suite", "root": "/tmp"}])


def test_resolve_activated_suite_root_rejects_unactivated_suite() -> None:
    """Unknown or inactive suite names must raise ValueError."""
    with pytest.raises(ValueError, match="not activated"):
        resolve_activated_suite_root("missing_suite", [])


def test_config_manager_get_activated_suite_root_after_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ConfigManager must expose activated Suite roots after reload."""
    home = tmp_path / "dataagent_home"
    suite_root = home / "suites" / "demo_suite"
    hooks_dir = suite_root / "hooks"
    hooks_dir.mkdir(parents=True)
    (suite_root / "suite.yaml").write_text(
        yaml.safe_dump({"name": "demo_suite", "enabled": True}, sort_keys=False),
        encoding="utf-8",
    )
    (hooks_dir / "hooks.yaml").write_text(
        yaml.safe_dump(
            {
                "HOOKS": {
                    "agent": {
                        "post": ["dataagent.core.flex.hooks.organize_workspace.organize_workspace"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATAAGENT_HOME", str(home))

    user_path = tmp_path / "user.yaml"
    user_path.write_text(
        yaml.safe_dump(
            {
                "AGENT_CONFIG": {"name": "ut", "type": "react"},
                "SUITE": {"include": ["demo_suite"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    cm = ConfigManager()
    cm.reload(str(user_path), str(DEFAULT_CONFIG))
    assert cm.get_activated_suite_root("demo_suite") == suite_root.resolve()


def test_config_manager_get_activated_suite_root_before_activation_raises() -> None:
    """Fresh ConfigManager without activated suites must reject lookups."""
    cm = ConfigManager()
    with pytest.raises(ValueError, match="not activated"):
        cm.get_activated_suite_root("example_suite")
