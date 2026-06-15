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
"""Unit tests for ``ConfigManager.reload`` Suite merge (no live LLM calls)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from dataagent.config.config_manager import ConfigManager
from dataagent.core.flex.flex_runtime_from_config import build_llm_configs_from_flex_config
from dataagent.utils.runtime_paths import dataagent_package_path

DEFAULT_CONFIG = dataagent_package_path("core", "flex", "flex_default_configs.yaml")


def _collect_hook_specs(hooks: Any) -> list[str]:
    """Flatten HOOKS tree leaf lists into comparable spec strings."""
    specs: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, str):
                    specs.append(item.strip())
                elif isinstance(item, dict):
                    name = str(item.get("name") or "").strip()
                    if name:
                        specs.append(name)

    _walk(hooks)
    return specs


def _install_minimal_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    enabled: bool = True,
    requires: list[str] | None = None,
    conflicts: list[str] | None = None,
    hooks_pre: str | None = None,
    priority: int = 0,
    hook_model: str | None = None,
) -> Path:
    """Create a minimal activatable Suite under a temporary user suites directory."""
    home = tmp_path / "dataagent_home"
    root = home / "suites" / name
    root.mkdir(parents=True)
    meta = {
        "name": name,
        "enabled": enabled,
        "priority": priority,
        "requires": requires or [],
        "conflicts": conflicts or [],
    }
    (root / "suite.yaml").write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")
    if hooks_pre:
        hooks_dir = root / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("", encoding="utf-8")
        (hooks_dir / "custom_hooks.py").write_text(
            "def hook(state, runtime):\n    return state\n",
            encoding="utf-8",
        )
        hook_item: dict[str, Any] | str = hooks_pre
        if hook_model:
            hook_item = {"name": hooks_pre, "model": hook_model}
        hooks_doc = {
            "HOOKS": {
                "nodes": {
                    "planner": {
                        "pre": [hook_item],
                    }
                }
            }
        }
        (hooks_dir / "hooks.yaml").write_text(yaml.safe_dump(hooks_doc), encoding="utf-8")
    monkeypatch.setenv("DATAAGENT_HOME", str(home))
    return root.resolve()


def _write_reload_user_config(
    tmp_path: Path,
    *,
    include: list[Any],
    with_model: bool = True,
) -> Path:
    """Write a minimal user YAML for ``ConfigManager.reload`` Suite tests."""
    payload: dict[str, Any] = {
        "AGENT_CONFIG": {"name": "suite-reload-ut", "type": "react"},
        "SUITE": {"include": include},
    }
    if with_model:
        payload["MODEL"] = {
            "chat_model": {
                "model_type": "chat",
                "provider": "bailian",
                "params": {"model": "deepseek-v4-flash"},
            }
        }
    path = tmp_path / "user_suite_reload.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_reload_same_priority_hook_order_alpha_before_beta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same priority: merged HOOKS list places lexicographically smaller suite name first."""
    _install_minimal_suite(
        tmp_path,
        monkeypatch,
        name="alpha_suite",
        priority=50,
        hooks_pre="hooks.custom_hooks.hook",
    )
    _install_minimal_suite(
        tmp_path,
        monkeypatch,
        name="beta_suite",
        priority=50,
        hooks_pre="hooks.custom_hooks.hook",
    )
    user_path = _write_reload_user_config(tmp_path, include=["beta_suite", "alpha_suite"])
    cm = ConfigManager()
    cm.reload(str(user_path), str(DEFAULT_CONFIG))
    specs = _collect_hook_specs(cm.settings.get("HOOKS", {}))
    alpha_idx = specs.index("alpha_suite.hooks.custom_hooks.hook")
    beta_idx = specs.index("beta_suite.hooks.custom_hooks.hook")
    pruner_idx = specs.index("pruner")
    assert alpha_idx < beta_idx < pruner_idx


def test_reload_priority_override_affects_hook_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``priority_override: 5`` pulls ``high_suite`` below ``low_suite`` (priority 10) in merge output."""
    _install_minimal_suite(
        tmp_path,
        monkeypatch,
        name="low_suite",
        priority=10,
        hooks_pre="hooks.custom_hooks.hook",
    )
    _install_minimal_suite(
        tmp_path,
        monkeypatch,
        name="high_suite",
        priority=100,
        hooks_pre="hooks.custom_hooks.hook",
    )
    user_path = _write_reload_user_config(
        tmp_path,
        include=["low_suite", {"name": "high_suite", "priority_override": 5}],
    )
    cm = ConfigManager()
    cm.reload(str(user_path), str(DEFAULT_CONFIG))
    specs = _collect_hook_specs(cm.settings.get("HOOKS", {}))
    high_idx = specs.index("high_suite.hooks.custom_hooks.hook")
    low_idx = specs.index("low_suite.hooks.custom_hooks.hook")
    assert low_idx < high_idx


def test_reload_suite_hook_model_registers_llm_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Suite hook dict ``model:`` registers ``llm_configs`` under merged hook name (env mocked)."""
    monkeypatch.setenv("BAILIAN_BASE_URL", "https://ut-suite/v1")
    monkeypatch.setenv("BAILIAN_API_KEY", "sk-ut-suite")
    _install_minimal_suite(
        tmp_path,
        monkeypatch,
        name="llm_suite",
        hooks_pre="hooks.custom_hooks.hook",
        hook_model="chat_model",
    )
    user_path = _write_reload_user_config(tmp_path, include=["llm_suite"])
    cm = ConfigManager()
    cm.reload(str(user_path), str(DEFAULT_CONFIG))
    hook_name = "llm_suite.hooks.custom_hooks.hook"
    llm_configs = build_llm_configs_from_flex_config(cm.settings)
    assert hook_name in llm_configs
    assert llm_configs[hook_name]["api_base"] == "https://ut-suite/v1"


def test_reload_partial_hooks_without_suite_succeeds(tmp_path: Path) -> None:
    """User partial HOOKS must not duplicate default hooks or fail strict duplicate validation."""
    user_path = tmp_path / "partial_hooks.yaml"
    user_path.write_text(
        yaml.safe_dump(
            {
                "AGENT_CONFIG": {"name": "partial-hooks-ut", "type": "react"},
                "HOOKS": {"nodes": {"executor": {"post": ["my_custom_hook"]}}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    cm = ConfigManager()
    cm.reload(str(user_path), str(DEFAULT_CONFIG))
    executor_post = cm.settings["HOOKS"]["nodes"]["executor"]["post"]
    assert executor_post == ["my_custom_hook"]
    planner_pre = cm.settings["HOOKS"]["nodes"]["planner"]["pre"]
    assert planner_pre == [{"name": "pruner"}]


def test_reload_failure_preserves_previous_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed reload must not mutate committed settings, activated_suites, or config_path."""
    import copy

    _install_minimal_suite(tmp_path, monkeypatch, name="good_suite", hooks_pre="hooks.custom_hooks.hook")
    good_path = _write_reload_user_config(tmp_path, include=["good_suite"])
    cm = ConfigManager()
    cm.reload(str(good_path), str(DEFAULT_CONFIG))
    previous_settings = copy.deepcopy(cm.settings)
    previous_suites = list(cm.activated_suites)
    previous_config_path = cm.config_path

    _install_minimal_suite(tmp_path, monkeypatch, name="left_suite", conflicts=["right_suite"])
    _install_minimal_suite(tmp_path, monkeypatch, name="right_suite")
    bad_path = _write_reload_user_config(tmp_path, include=["left_suite", "right_suite"])
    with pytest.raises(ValueError, match="conflicts"):
        cm.reload(str(bad_path), str(DEFAULT_CONFIG))

    assert cm.settings == previous_settings
    assert cm.activated_suites == previous_suites
    assert cm.config_path == previous_config_path


def test_reload_conflicting_suites_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutually exclusive Suites fail during ``ConfigManager.reload`` activation."""
    _install_minimal_suite(tmp_path, monkeypatch, name="left_suite", conflicts=["right_suite"])
    _install_minimal_suite(tmp_path, monkeypatch, name="right_suite")
    user_path = _write_reload_user_config(tmp_path, include=["left_suite", "right_suite"])
    cm = ConfigManager()
    with pytest.raises(ValueError, match="conflicts"):
        cm.reload(str(user_path), str(DEFAULT_CONFIG))
