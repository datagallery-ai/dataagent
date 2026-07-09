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
"""Integration tests: Suite discovery, activation, and ``ConfigManager.reload`` merge."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from dataagent.config.config_manager import ConfigManager
from dataagent.core.flex.flex_runtime_from_config import build_llm_configs_from_flex_config
from dataagent.core.managers.action_manager.manager import ToolManager
from dataagent.core.suite.activation import activate_suites
from dataagent.core.suite.discovery import discover_suite_index
from dataagent.utils.runtime_paths import dataagent_package_path

DEFAULT_CONFIG = dataagent_package_path("core", "flex", "flex_default_configs.yaml")
BUILTIN_EXAMPLE_ROOT = dataagent_package_path("core", "suite", "builtin_suites", "example_suite")
EXAMPLE_SYSTEM_PROMPT_PATH = BUILTIN_EXAMPLE_ROOT / "prompts" / "system" / "example_suite1.md"
EXAMPLE_USER_PROMPT_PATH = BUILTIN_EXAMPLE_ROOT / "prompts" / "user" / "example_suite_user1.md"
EXAMPLE_ARITHMETIC_SUBAGENT_PATH = BUILTIN_EXAMPLE_ROOT / "subagents" / "arithmetic_ref.yaml"
EXAMPLE_ECHO_SUBAGENT_PATH = BUILTIN_EXAMPLE_ROOT / "subagents" / "echo_ref.yaml"


def _write_user_config(tmp_path: Path, *, suite_name: str = "example_suite") -> Path:
    """Write a minimal react user YAML that requests one Suite in ``SUITE.include``."""
    payload: dict[str, Any] = {
        "AGENT_CONFIG": {
            "name": "suite-integration-test",
            "type": "react",
        },
        "SUITE": {
            "include": [suite_name],
        },
        "MODEL": {
            "chat_model": {
                "model_type": "chat",
                "provider": "bailian",
                "params": {
                    "model": "deepseek-v4-flash",
                },
            }
        },
    }
    path = tmp_path / "user_with_example_suite.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _install_example_suite_override(
    tmp_path: Path,
    monkeypatch,
    *,
    enabled: bool,
) -> Path:
    """
    Copy ``example_suite`` into a temporary user suites dir with a chosen ``enabled`` flag.

    User-level paths outrank ``builtin_suites`` so tests do not depend on shipped
    ``suite.yaml`` ``enabled`` value.
    """
    home = tmp_path / "dataagent_home"
    target = home / "suites" / "example_suite"
    shutil.copytree(BUILTIN_EXAMPLE_ROOT, target)
    meta_path = target / "suite.yaml"
    meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    meta["enabled"] = enabled
    meta_path.write_text(yaml.safe_dump(meta, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("DATAAGENT_HOME", str(home))
    return target.resolve()


def _install_activatable_example_suite(tmp_path: Path, monkeypatch) -> Path:
    """Install user-level ``example_suite`` copy with ``enabled: true`` for merge tests."""
    return _install_example_suite_override(tmp_path, monkeypatch, enabled=True)


def _install_disabled_example_suite(tmp_path: Path, monkeypatch) -> Path:
    """Install user-level ``example_suite`` copy with ``enabled: false`` for skip tests."""
    return _install_example_suite_override(tmp_path, monkeypatch, enabled=False)


def _local_function_names(settings: dict[str, Any]) -> list[str]:
    """Return registered local function names from merged TOOLS config."""
    tools = settings.get("TOOLS", {})
    if not isinstance(tools, dict):
        return []
    entries = tools.get("local_functions", [])
    if not isinstance(entries, list):
        return []
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("function") or "").strip()
        if name:
            names.append(name)
    return names


def _planner_node(settings: dict[str, Any]) -> dict[str, Any]:
    """Return the merged ACTOR_LOOP planner node dict."""
    for item in settings.get("ACTOR_LOOP", []):
        if isinstance(item, dict) and item.get("node") == "planner":
            return item
    raise AssertionError("planner node not found in ACTOR_LOOP")


def _executor_node(settings: dict[str, Any]) -> dict[str, Any]:
    """Return the merged ACTOR_LOOP executor node dict."""
    for item in settings.get("ACTOR_LOOP", []):
        if isinstance(item, dict) and item.get("node") == "executor":
            return item
    raise AssertionError("executor node not found in ACTOR_LOOP")


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


def test_discover_builtin_example_suite_indexed() -> None:
    """Reference ``example_suite`` is indexed from ``builtin_suites``."""
    index = discover_suite_index()
    assert "example_suite" in index
    entry = index["example_suite"]
    assert entry.root == BUILTIN_EXAMPLE_ROOT.resolve()


def test_activate_disabled_example_suite_is_skipped(tmp_path, monkeypatch) -> None:
    """Explicit ``SUITE.include`` of a disabled Suite does not activate it."""
    _install_disabled_example_suite(tmp_path, monkeypatch)
    index = discover_suite_index()
    activated = activate_suites(suite_config={"include": ["example_suite"]}, index=index)
    assert activated == []


def test_activate_user_override_example_suite(tmp_path, monkeypatch) -> None:
    """User-level copy with ``enabled: true`` activates and wins over builtin template."""
    user_root = _install_activatable_example_suite(tmp_path, monkeypatch)
    index = discover_suite_index()
    activated = activate_suites(suite_config={"include": ["example_suite"]}, index=index)
    assert [suite.name for suite in activated] == ["example_suite"]
    assert activated[0].root == user_root


def test_reload_skips_disabled_example_suite(tmp_path, monkeypatch) -> None:
    """User-level ``example_suite`` with ``enabled: false`` produces no merge contributions."""
    _install_disabled_example_suite(tmp_path, monkeypatch)
    user_path = _write_user_config(tmp_path)
    cm = ConfigManager()
    cm.reload(str(user_path), str(DEFAULT_CONFIG))

    assert cm.activated_suites == []
    hook_specs = _collect_hook_specs(cm.settings.get("HOOKS", {}))
    assert "example_suite.hooks.custom_hooks.suite_example_pre" not in hook_specs


def _hook_dict_with_name(hooks: Any, target_name: str) -> dict[str, Any] | None:
    """Find a dict-shaped HOOKS list entry whose ``name`` equals ``target_name``."""
    items: list[Any] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    items.append(item)
                _walk(item)

    _walk(hooks)
    for item in items:
        if str(item.get("name") or "").strip() == target_name:
            return item
    return None


def test_reload_merges_user_enabled_example_suite(tmp_path, monkeypatch) -> None:
    """``ConfigManager.reload`` merges all example_suite layer contributions."""
    monkeypatch.setenv("BAILIAN_BASE_URL", "https://suite-test/v1")
    monkeypatch.setenv("BAILIAN_API_KEY", "sk-suite-test")
    user_root = _install_activatable_example_suite(tmp_path, monkeypatch)
    user_path = _write_user_config(tmp_path)
    cm = ConfigManager()
    cm.reload(str(user_path), str(DEFAULT_CONFIG))

    assert cm.activated_suites == [{"name": "example_suite", "root": str(user_root)}]
    assert "SUITE" not in cm.settings

    hook_specs = _collect_hook_specs(cm.settings.get("HOOKS", {}))
    assert "example_suite.hooks.custom_hooks.suite_example_pre" in hook_specs
    assert "example_suite.hooks.custom_hooks.suite_example_post" in hook_specs
    assert "example_suite.hooks.custom_hooks.suite_example_with_model" in hook_specs
    assert "pruner" in hook_specs

    with_model = _hook_dict_with_name(
        cm.settings.get("HOOKS", {}),
        "example_suite.hooks.custom_hooks.suite_example_with_model",
    )
    assert with_model is not None
    assert with_model.get("model") == "chat_model"

    hook_llm_key = "example_suite.hooks.custom_hooks.suite_example_with_model"
    llm_configs = build_llm_configs_from_flex_config(cm.settings)
    assert hook_llm_key in llm_configs
    assert llm_configs[hook_llm_key]["api_base"] == "https://suite-test/v1"

    model = cm.settings.get("MODEL", {})
    chat_model = model.get("chat_model", {}) if isinstance(model, dict) else {}
    params = chat_model.get("params", {}) if isinstance(chat_model, dict) else {}
    assert params.get("temperature") == 0.1

    assert "example_suite_file_saver" in _local_function_names(cm.settings)

    planner = _planner_node(cm.settings)
    prompt_template = planner.get("prompt_template", {})
    system_specs = prompt_template.get("system", []) if isinstance(prompt_template, dict) else []
    user_specs = prompt_template.get("user", []) if isinstance(prompt_template, dict) else []
    system_paths = [str(spec.get("path", "")) for spec in system_specs if isinstance(spec, dict) and spec.get("path")]
    user_paths = [str(spec.get("path", "")) for spec in user_specs if isinstance(spec, dict) and spec.get("path")]
    assert any(path.endswith("example_suite1.md") for path in system_paths)
    assert any(path.endswith("example_suite_user1.md") for path in user_paths)
    assert "[SUITE-EXAMPLE-SYSTEM-APPEND]" in EXAMPLE_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    assert "[SUITE-EXAMPLE-USER-APPEND]" in EXAMPLE_USER_PROMPT_PATH.read_text(encoding="utf-8")

    executor = _executor_node(cm.settings)
    assert executor.get("max_tool_result_length") == 8192

    skills = cm.settings.get("TOOLS", {}).get("skills", {})
    assert "builtin" not in skills or not skills.get("builtin")
    assert any(str(p).endswith("example_suite/skills") for p in skills.get("custom_dirs", []))

    subagents = cm.settings.get("SUBAGENT_CONFIGS", [])
    subagent_paths = [
        str(entry.get("path", "")) for entry in subagents if isinstance(entry, dict) and entry.get("path")
    ]
    assert any(path.endswith("arithmetic_ref.yaml") for path in subagent_paths)
    assert any(path.endswith("echo_ref.yaml") for path in subagent_paths)
    assert EXAMPLE_ARITHMETIC_SUBAGENT_PATH.is_file()
    assert EXAMPLE_ECHO_SUBAGENT_PATH.is_file()

    resources = cm.settings.get("RESOURCES", [])
    assert isinstance(resources, list)
    assert any(isinstance(item, dict) and item.get("id") == "local" for item in resources)
    local_resource = next(item for item in resources if item.get("id") == "local")
    assert local_resource.get("transport", {}).get("type") == "local"
    assert local_resource.get("operations", {}).get("submit") == "sandbox.submit"

    tm = ToolManager()
    tm._register_implicit_job_tools(cm.settings)
    for name in ("submit_resource_job", "poll_job", "collect_job", "cancel_job"):
        assert tm.exists(name)


def _install_minimal_suite(
    tmp_path: Path,
    monkeypatch,
    *,
    name: str,
    enabled: bool = True,
    requires: list[str] | None = None,
    conflicts: list[str] | None = None,
    hooks_pre: str | None = None,
    priority: int = 0,
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
        hooks_doc = {
            "HOOKS": {
                "nodes": {
                    "planner": {
                        "pre": [hooks_pre],
                    }
                }
            }
        }
        (hooks_dir / "hooks.yaml").write_text(yaml.safe_dump(hooks_doc), encoding="utf-8")
    monkeypatch.setenv("DATAAGENT_HOME", str(home))
    return root.resolve()


def test_reload_requires_closure_activates_dependency(tmp_path, monkeypatch) -> None:
    """``ConfigManager.reload`` pulls in ``requires`` dependencies automatically."""
    dep_root = _install_minimal_suite(tmp_path, monkeypatch, name="dep_suite", hooks_pre="hooks.custom_hooks.hook")
    base_root = _install_minimal_suite(
        tmp_path,
        monkeypatch,
        name="base_suite",
        requires=["dep_suite"],
        hooks_pre="hooks.custom_hooks.hook",
    )
    payload = {
        "AGENT_CONFIG": {"name": "requires-test", "type": "react"},
        "SUITE": {"include": ["base_suite"]},
    }
    user_path = tmp_path / "requires_user.yaml"
    user_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    cm = ConfigManager()
    cm.reload(str(user_path), str(DEFAULT_CONFIG))
    names = {entry["name"] for entry in cm.activated_suites}
    assert names == {"dep_suite", "base_suite"}
    hook_specs = _collect_hook_specs(cm.settings.get("HOOKS", {}))
    assert "dep_suite.hooks.custom_hooks.hook" in hook_specs
    assert "base_suite.hooks.custom_hooks.hook" in hook_specs
    assert str(dep_root) in {entry["root"] for entry in cm.activated_suites}
    assert str(base_root) in {entry["root"] for entry in cm.activated_suites}


def test_reload_conflicting_suites_raises(tmp_path, monkeypatch) -> None:
    """Mutually exclusive Suites fail during ``ConfigManager.reload`` activation."""
    _install_minimal_suite(tmp_path, monkeypatch, name="left_suite", conflicts=["right_suite"])
    _install_minimal_suite(tmp_path, monkeypatch, name="right_suite")
    payload = {
        "AGENT_CONFIG": {"name": "conflict-test", "type": "react"},
        "SUITE": {"include": ["left_suite", "right_suite"]},
    }
    user_path = tmp_path / "conflict_user.yaml"
    user_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    cm = ConfigManager()
    with pytest.raises(ValueError, match="conflicts"):
        cm.reload(str(user_path), str(DEFAULT_CONFIG))


def test_reload_unknown_suite_raises(tmp_path) -> None:
    """Missing Suite name fails fast during activation."""
    payload = {
        "AGENT_CONFIG": {"name": "x", "type": "react"},
        "SUITE": {"include": ["nonexistent_suite_xyz"]},
    }
    user_path = tmp_path / "bad_suite.yaml"
    user_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    cm = ConfigManager()
    try:
        cm.reload(str(user_path), str(DEFAULT_CONFIG))
        raised = False
    except ValueError as exc:
        raised = True
        assert "nonexistent_suite_xyz" in str(exc)
    assert raised
