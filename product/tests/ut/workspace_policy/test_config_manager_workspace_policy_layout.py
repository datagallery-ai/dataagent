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
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from dataagent.config.config_manager import ConfigManager
from dataagent.utils.constants import DEFAULT_WORKSPACE_LAYOUT
from dataagent.utils.runtime_paths import dataagent_package_path

DEFAULT_CONFIG = dataagent_package_path("core", "flex", "flex_default_configs.yaml")


def _write_yaml(path: Path, payload: dict) -> Path:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_reload_merges_workspace_policy_layout_from_default_and_user(tmp_path: Path) -> None:
    user_path = _write_yaml(
        tmp_path / "layout_user.yaml",
        {
            "AGENT_CONFIG": {"name": "layout-user", "type": "react"},
            "WORKSPACE_POLICY": {"layout": {"session_memory_dir": "state/mem"}},
        },
    )
    cm = ConfigManager()
    cm.reload(str(user_path), str(DEFAULT_CONFIG))
    layout = cm.settings["WORKSPACE_POLICY"]["layout"]
    assert layout["session_memory_dir"] == "state/mem"
    assert layout["context_dir"] == DEFAULT_WORKSPACE_LAYOUT["context_dir"]


def test_reload_rejects_absolute_layout_segment(tmp_path: Path) -> None:
    good_path = _write_yaml(
        tmp_path / "good_layout.yaml",
        {"AGENT_CONFIG": {"name": "good", "type": "react"}},
    )
    bad_path = _write_yaml(
        tmp_path / "bad_layout.yaml",
        {
            "AGENT_CONFIG": {"name": "bad", "type": "react"},
            "WORKSPACE_POLICY": {"layout": {"session_memory_dir": "/abs/path"}},
        },
    )
    cm = ConfigManager()
    cm.reload(str(good_path), str(DEFAULT_CONFIG))
    previous = copy.deepcopy(cm.settings)
    with pytest.raises(ValueError, match="relative path segment"):
        cm.reload(str(bad_path), str(DEFAULT_CONFIG))
    assert cm.settings == previous


def test_reload_rejects_layout_segment_with_dotdot(tmp_path: Path) -> None:
    good_path = _write_yaml(
        tmp_path / "good_layout2.yaml",
        {"AGENT_CONFIG": {"name": "good", "type": "react"}},
    )
    bad_path = _write_yaml(
        tmp_path / "bad_layout2.yaml",
        {
            "AGENT_CONFIG": {"name": "bad", "type": "react"},
            "WORKSPACE_POLICY": {"layout": {"context_dir": "../escape"}},
        },
    )
    cm = ConfigManager()
    cm.reload(str(good_path), str(DEFAULT_CONFIG))
    previous = copy.deepcopy(cm.settings)
    with pytest.raises(ValueError, match="must not contain"):
        cm.reload(str(bad_path), str(DEFAULT_CONFIG))
    assert cm.settings == previous
