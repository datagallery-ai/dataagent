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
"""Tests for per-Suite layer preprocessing."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dataagent.core.suite.suite_layer import build_suite_layers
from dataagent.core.suite.types import SuiteRecord


def _suite_record(tmp_path: Path, name: str = "test_suite") -> SuiteRecord:
    """Build a ``SuiteRecord`` rooted at ``tmp_path``."""
    (tmp_path / "suite.yaml").write_text(yaml.safe_dump({"name": name}), encoding="utf-8")
    return SuiteRecord(
        name=name,
        root=tmp_path,
        priority=0,
        enabled=True,
        meta={},
    )


def test_node_configs_unknown_actor_loop_node_raises(tmp_path: Path) -> None:
    """Suite ``node_configs`` must not reference unknown default ACTOR_LOOP nodes."""
    suite_root = tmp_path / "bad_suite"
    suite_root.mkdir()
    (suite_root / "suite.yaml").write_text(yaml.safe_dump({"name": "bad_suite"}), encoding="utf-8")
    (suite_root / "node_configs.yaml").write_text(
        yaml.safe_dump({"unknown_node": {"max_tool_result_length": 4096}}),
        encoding="utf-8",
    )
    suite = SuiteRecord(name="bad_suite", root=suite_root, priority=0, enabled=True, meta={})
    with pytest.raises(ValueError, match="unknown ACTOR_LOOP nodes"):
        build_suite_layers([suite], default_actor_nodes={"planner", "executor"})


def test_prompts_unknown_actor_loop_node_raises(tmp_path: Path) -> None:
    """Suite ``prompts/`` patches must target an existing planner node only."""
    suite_root = tmp_path / "prompt_suite"
    prompts = suite_root / "prompts" / "system"
    prompts.mkdir(parents=True)
    (prompts / "extra.md").write_text("append", encoding="utf-8")
    suite = _suite_record(suite_root, name="prompt_suite")
    with pytest.raises(ValueError, match="unknown ACTOR_LOOP nodes"):
        build_suite_layers([suite], default_actor_nodes={"executor"})
