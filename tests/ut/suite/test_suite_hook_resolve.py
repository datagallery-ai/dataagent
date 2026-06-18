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
"""Tests for Suite hook import and Flex hook signature validation."""

import pytest

from dataagent.core.cbb.base_agent import BaseAgent
from dataagent.core.flex.agent import FlexAgent
from dataagent.utils.runtime_paths import dataagent_package_path

EXAMPLE_SUITE_ROOT = dataagent_package_path("core", "suite", "builtin_suites", "example_suite")


def test_example_suite_hook_import_passes_flex_validation() -> None:
    """``example_suite`` reference hook must satisfy ``(state, runtime)`` contract."""
    fn = FlexAgent.import_hook_from_suite_root(
        "hooks.custom_hooks.suite_example_pre",
        root=EXAMPLE_SUITE_ROOT,
        suite_name="example_suite",
        location="nodes.planner.pre",
    )
    BaseAgent._validate_hook(fn, "nodes.planner.pre")


def test_suite_hook_import_isolated_between_suites(tmp_path) -> None:
    """Two Suites with the same relative hook path must resolve distinct callables."""
    alpha_root = tmp_path / "alpha_suite"
    beta_root = tmp_path / "beta_suite"
    for _name, root, marker in (
        ("alpha_suite", alpha_root, "alpha"),
        ("beta_suite", beta_root, "beta"),
    ):
        hooks_dir = root / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "custom_hooks.py").write_text(
            f"def hook(state, runtime):\n    state['marker'] = {marker!r}\n    return state\n",
            encoding="utf-8",
        )
    alpha_fn = FlexAgent.import_hook_from_suite_root(
        "hooks.custom_hooks.hook",
        root=alpha_root,
        suite_name="alpha_suite",
        location="ut.alpha",
    )
    beta_fn = FlexAgent.import_hook_from_suite_root(
        "hooks.custom_hooks.hook",
        root=beta_root,
        suite_name="beta_suite",
        location="ut.beta",
    )
    assert alpha_fn is not beta_fn
    assert alpha_fn({"marker": ""}, None)["marker"] == "alpha"
    assert beta_fn({"marker": ""}, None)["marker"] == "beta"


def test_suite_hook_prefix_match_prefers_longer_suite_name(tmp_path) -> None:
    """Hook resolution must match the longest activated suite name prefix."""
    foo_root = tmp_path / "foo"
    foo_bar_root = tmp_path / "foo_bar"
    for _name, root, marker in (
        ("foo", foo_root, "foo"),
        ("foo_bar", foo_bar_root, "foo_bar"),
    ):
        hooks_dir = root / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "custom_hooks.py").write_text(
            f"def hook(state, runtime):\n    state['marker'] = {marker!r}\n    return state\n",
            encoding="utf-8",
        )

    agent = object.__new__(FlexAgent)
    agent.config_manager = type(
        "_CM",
        (),
        {
            "activated_suites": [
                {"name": "foo", "root": str(foo_root)},
                {"name": "foo_bar", "root": str(foo_bar_root)},
            ]
        },
    )()
    fn = agent._try_resolve_suite_hook(
        "foo_bar.hooks.custom_hooks.hook",
        location="ut.foo_bar",
    )
    assert fn is not None
    assert fn({"marker": ""}, None)["marker"] == "foo_bar"


def test_resolve_hook_callable_loads_example_suite_hook() -> None:
    """FlexAgent hook resolution must load an activated example_suite hook callable."""
    agent = object.__new__(FlexAgent)
    agent.config_manager = type(
        "_CM",
        (),
        {
            "activated_suites": [
                {
                    "name": "example_suite",
                    "root": str(EXAMPLE_SUITE_ROOT),
                }
            ]
        },
    )()
    fn = agent._resolve_hook_callable(
        "example_suite.hooks.custom_hooks.suite_example_pre",
        location="nodes.planner.pre",
    )
    BaseAgent._validate_hook(fn, "nodes.planner.pre")


def test_resolve_hook_callable_loads_framework_hook_from_suite_merge() -> None:
    """Merged Suite ``dataagent.*`` hook specs must resolve via ``resolve_builtin_hook``."""
    framework_hook = "dataagent.core.flex.hooks.organize_workspace.organize_workspace"
    agent = object.__new__(FlexAgent)
    agent.config_manager = type("_CM", (), {"activated_suites": []})()
    fn = agent._resolve_hook_callable(framework_hook, location="agent.post")
    BaseAgent._validate_hook(fn, "agent.post")


def test_suite_hook_relative_import_fails(tmp_path) -> None:
    """Package-relative imports inside Suite hook files are not supported."""
    root = tmp_path / "rel_import_suite"
    hooks_dir = root / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "common.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (hooks_dir / "custom_hooks.py").write_text(
        "from hooks.common import helper\ndef hook(state, runtime):\n    return state\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="failed to import Suite hook"):
        FlexAgent.import_hook_from_suite_root(
            "hooks.custom_hooks.hook",
            root=root,
            suite_name="rel_import_suite",
            location="ut.relative",
        )
