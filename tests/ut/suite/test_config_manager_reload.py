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
"""Tests for ``ConfigManager.reload`` interpolation and rollback semantics."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from dataagent.config.config_manager import ConfigManager
from dataagent.utils.runtime_paths import dataagent_package_path

DEFAULT_CONFIG = dataagent_package_path("core", "flex", "flex_default_configs.yaml")


def _write_yaml(path: Path, payload: dict) -> Path:
    """Write a YAML mapping to ``path`` and return the path."""
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_reload_interpolation_resolves_from_working_on_first_load(tmp_path: Path) -> None:
    """``${...}`` must resolve against in-flight ``working``, not empty ``self.settings``."""
    user_path = _write_yaml(
        tmp_path / "interp_first.yaml",
        {
            "AGENT_CONFIG": {"name": "interp-ut", "type": "react"},
            "MODEL": {
                "chat_model": {
                    "model_type": "chat",
                    "provider": "openai",
                    "params": {"model": "gpt-test"},
                }
            },
            "ACTOR_LOOP": [
                {
                    "node": "planner",
                    "module": "dataagent.core.flex.nodes.planner.Planner",
                    "chat_model": {"name": "${MODEL.chat_model.params.model}"},
                }
            ],
        },
    )
    cm = ConfigManager()
    cm.reload(str(user_path), str(DEFAULT_CONFIG))
    planner = next(node for node in cm.settings["ACTOR_LOOP"] if node["node"] == "planner")
    assert planner["chat_model"]["name"] == "gpt-test"


def test_reload_interpolation_uses_new_config_not_stale_settings(tmp_path: Path) -> None:
    """Second reload must not resolve ``${...}`` from the previous committed settings."""
    first_path = _write_yaml(
        tmp_path / "interp_old.yaml",
        {
            "AGENT_CONFIG": {"name": "interp-old", "type": "react"},
            "MODEL": {
                "chat_model": {
                    "model_type": "chat",
                    "provider": "openai",
                    "params": {"model": "old-model"},
                }
            },
            "ACTOR_LOOP": [
                {
                    "node": "planner",
                    "module": "dataagent.core.flex.nodes.planner.Planner",
                    "chat_model": {"name": "${MODEL.chat_model.params.model}"},
                }
            ],
        },
    )
    second_path = _write_yaml(
        tmp_path / "interp_new.yaml",
        {
            "AGENT_CONFIG": {"name": "interp-new", "type": "react"},
            "MODEL": {
                "chat_model": {
                    "model_type": "chat",
                    "provider": "openai",
                    "params": {"model": "new-model"},
                }
            },
            "ACTOR_LOOP": [
                {
                    "node": "planner",
                    "module": "dataagent.core.flex.nodes.planner.Planner",
                    "chat_model": {"name": "${MODEL.chat_model.params.model}"},
                }
            ],
        },
    )
    cm = ConfigManager()
    cm.reload(str(first_path), str(DEFAULT_CONFIG))
    cm.reload(str(second_path), str(DEFAULT_CONFIG))
    planner = next(node for node in cm.settings["ACTOR_LOOP"] if node["node"] == "planner")
    assert planner["chat_model"]["name"] == "new-model"


def test_reload_failure_on_duplicate_hooks_preserves_settings(tmp_path: Path) -> None:
    """Strict duplicate validation failure must not mutate committed settings."""
    good_path = _write_yaml(
        tmp_path / "good.yaml",
        {
            "AGENT_CONFIG": {"name": "good", "type": "react"},
            "HOOKS": {"nodes": {"planner": {"pre": ["my_hook"]}}},
        },
    )
    bad_path = _write_yaml(
        tmp_path / "bad.yaml",
        {
            "AGENT_CONFIG": {"name": "bad", "type": "react"},
            "HOOKS": {"nodes": {"planner": {"pre": ["pruner", "pruner"]}}},
        },
    )
    cm = ConfigManager()
    cm.reload(str(good_path), str(DEFAULT_CONFIG))
    previous = copy.deepcopy(cm.settings)
    with pytest.raises(ValueError, match="Duplicate"):
        cm.reload(str(bad_path), str(DEFAULT_CONFIG))
    assert cm.settings == previous


def test_reload_failure_on_relative_workspace_preserves_settings(tmp_path: Path) -> None:
    """WORKSPACE validation failure must not mutate committed settings."""
    good_path = _write_yaml(
        tmp_path / "good_ws.yaml",
        {"AGENT_CONFIG": {"name": "good", "type": "react"}},
    )
    bad_path = _write_yaml(
        tmp_path / "bad_ws.yaml",
        {
            "AGENT_CONFIG": {"name": "bad", "type": "react"},
            "WORKSPACE": {"path": "relative/not-allowed"},
        },
    )
    cm = ConfigManager()
    cm.reload(str(good_path), str(DEFAULT_CONFIG))
    previous = copy.deepcopy(cm.settings)
    with pytest.raises(ValueError, match="absolute path"):
        cm.reload(str(bad_path), str(DEFAULT_CONFIG))
    assert cm.settings == previous


def test_reload_empty_hook_list_end_to_end(tmp_path: Path) -> None:
    """User ``post: []`` must not duplicate default hooks after full reload."""
    user_path = _write_yaml(
        tmp_path / "empty_hook_list.yaml",
        {
            "AGENT_CONFIG": {"name": "empty-hooks", "type": "react"},
            "HOOKS": {"nodes": {"executor": {"post": []}}},
        },
    )
    cm = ConfigManager()
    cm.reload(str(user_path), str(DEFAULT_CONFIG))
    assert cm.settings["HOOKS"]["nodes"]["executor"]["post"] == []
