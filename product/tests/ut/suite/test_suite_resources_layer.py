# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Suite resources layer loading tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dataagent.core.managers.action_manager.manager import ToolManager
from dataagent.core.suite.merge import merge_layers
from dataagent.core.suite.suite_layer import build_suite_layers
from dataagent.core.suite.types import SuiteRecord
from dataagent.core.suite.validation import validate_strict_duplicates


def test_load_resources_layer_from_suite_root(tmp_path: Path):
    """build_suite_layers includes RESOURCES from resources/resources.yaml."""
    resources_dir = tmp_path / "resources"
    resources_dir.mkdir()
    (resources_dir / "resources.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "id": "local",
                    "category": "executable",
                    "transport": {"type": "local"},
                    "operations": {
                        "submit": "sandbox.submit",
                        "poll": "sandbox.poll",
                        "collect": "sandbox.collect",
                        "cancel": "sandbox.cancel",
                    },
                    "capacity": {"total": 2, "unit": "slot"},
                    "consumption": {"*": 1},
                }
            ]
        ),
        encoding="utf-8",
    )
    suite = SuiteRecord(name="demo_suite", root=tmp_path, priority=0, enabled=True)
    layers, _meta = build_suite_layers([suite], default_actor_nodes={"planner", "executor"})
    assert len(layers) == 1
    assert layers[0]["RESOURCES"][0]["id"] == "local"


def test_load_resources_layer_accepts_resources_key_wrapper(tmp_path: Path):
    """build_suite_layers accepts resources/resources.yaml with top-level RESOURCES key."""
    resources_dir = tmp_path / "resources"
    resources_dir.mkdir()
    (resources_dir / "resources.yaml").write_text(
        yaml.safe_dump(
            {
                "RESOURCES": [
                    {
                        "id": "local",
                        "category": "executable",
                        "transport": {"type": "local"},
                        "operations": {
                            "submit": "sandbox.submit",
                            "poll": "sandbox.poll",
                            "collect": "sandbox.collect",
                            "cancel": "sandbox.cancel",
                        },
                        "capacity": {"total": 2, "unit": "slot"},
                        "consumption": {"*": 1},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    suite = SuiteRecord(name="demo_suite", root=tmp_path, priority=0, enabled=True)
    layers, _meta = build_suite_layers([suite], default_actor_nodes={"planner", "executor"})
    assert layers[0]["RESOURCES"][0]["id"] == "local"


def test_load_resources_layer_ignores_mapping_without_resources_key(tmp_path: Path):
    """Mapping without RESOURCES/resources key contributes nothing (like empty file)."""
    resources_dir = tmp_path / "resources"
    resources_dir.mkdir()
    (resources_dir / "resources.yaml").write_text("id: local\n", encoding="utf-8")
    suite = SuiteRecord(name="demo_suite", root=tmp_path, priority=0, enabled=True)
    layers, _meta = build_suite_layers([suite], default_actor_nodes={"planner", "executor"})
    assert layers == []


def test_load_resources_layer_rejects_non_list_resources_key(tmp_path: Path):
    """RESOURCES key must map to a list when present."""
    resources_dir = tmp_path / "resources"
    resources_dir.mkdir()
    (resources_dir / "resources.yaml").write_text(
        yaml.safe_dump({"RESOURCES": {"id": "local"}}),
        encoding="utf-8",
    )
    suite = SuiteRecord(name="demo_suite", root=tmp_path, priority=0, enabled=True)
    with pytest.raises(ValueError, match="Suite 'demo_suite' resources invalid: RESOURCES must be a YAML list"):
        build_suite_layers([suite], default_actor_nodes={"planner", "executor"})


def test_load_resources_layer_rejects_scalar_root(tmp_path: Path):
    """Scalar YAML roots are rejected."""
    resources_dir = tmp_path / "resources"
    resources_dir.mkdir()
    (resources_dir / "resources.yaml").write_text("not-a-mapping\n", encoding="utf-8")
    suite = SuiteRecord(name="demo_suite", root=tmp_path, priority=0, enabled=True)
    with pytest.raises(ValueError, match="Suite 'demo_suite' resources invalid: resources file must be a YAML list or"):
        build_suite_layers([suite], default_actor_nodes={"planner", "executor"})


def test_load_resources_layer_includes_suite_name_on_schema_error(tmp_path: Path):
    """Invalid executable resources fail fast with suite name and file path."""
    resources_dir = tmp_path / "resources"
    resources_dir.mkdir()
    (resources_dir / "resources.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "id": "broken",
                    "category": "executable",
                    "capacity": {"total": 1, "unit": "slot"},
                    "consumption": {"*": 1},
                }
            ]
        ),
        encoding="utf-8",
    )
    suite = SuiteRecord(name="demo_suite", root=tmp_path, priority=0, enabled=True)
    with pytest.raises(ValueError, match="Suite 'demo_suite' resources invalid:.*transport.type"):
        build_suite_layers([suite], default_actor_nodes={"planner", "executor"})


def test_merge_layers_appends_resources_in_priority_order():
    """Higher-priority layers prepend RESOURCES entries during merge."""
    default = {
        "RESOURCES": [
            {"id": "base", "category": "non-executable", "capacity": {"total": 1, "unit": "x"}, "consumption": {"*": 1}}
        ]
    }
    suite_layer = {
        "RESOURCES": [
            {
                "id": "suite_local",
                "category": "executable",
                "transport": {"type": "local"},
                "operations": {
                    "submit": "sandbox.submit",
                    "poll": "sandbox.poll",
                    "collect": "sandbox.collect",
                    "cancel": "sandbox.cancel",
                },
                "capacity": {"total": 2, "unit": "slot"},
                "consumption": {"*": 1},
            }
        ]
    }
    user_layer = {
        "RESOURCES": [
            {
                "id": "user_local",
                "category": "executable",
                "transport": {"type": "local"},
                "operations": {
                    "submit": "sandbox.submit",
                    "poll": "sandbox.poll",
                    "collect": "sandbox.collect",
                    "cancel": "sandbox.cancel",
                },
                "capacity": {"total": 1, "unit": "slot"},
                "consumption": {"*": 1},
            }
        ]
    }
    merged = merge_layers([default, suite_layer, user_layer])
    ids = [item["id"] for item in merged["RESOURCES"]]
    assert ids == ["user_local", "suite_local", "base"]


def test_duplicate_resource_id_fails_validation():
    """Merged RESOURCES with duplicate ids must fail strict validation."""
    config = {
        "RESOURCES": [
            {
                "id": "local",
                "category": "executable",
                "transport": {"type": "local"},
                "operations": {
                    "submit": "sandbox.submit",
                    "poll": "sandbox.poll",
                    "collect": "sandbox.collect",
                    "cancel": "sandbox.cancel",
                },
                "capacity": {"total": 1, "unit": "slot"},
                "consumption": {"*": 1},
            },
            {
                "id": "local",
                "category": "executable",
                "transport": {"type": "local"},
                "operations": {
                    "submit": "sandbox.submit",
                    "poll": "sandbox.poll",
                    "collect": "sandbox.collect",
                    "cancel": "sandbox.cancel",
                },
                "capacity": {"total": 1, "unit": "slot"},
                "consumption": {"*": 1},
            },
        ]
    }
    with pytest.raises(ValueError, match="Duplicate RESOURCES.id"):
        validate_strict_duplicates(config)


def test_resources_register_implicit_job_tools():
    """Non-empty RESOURCES registers resource lifecycle tools."""
    tm = ToolManager()
    tm._register_implicit_job_tools(
        {
            "RESOURCES": [
                {
                    "id": "local",
                    "category": "executable",
                    "transport": {"type": "local"},
                    "operations": {
                        "submit": "sandbox.submit",
                        "poll": "sandbox.poll",
                        "collect": "sandbox.collect",
                        "cancel": "sandbox.cancel",
                    },
                    "capacity": {"total": 1, "unit": "slot"},
                    "consumption": {"*": 1},
                }
            ]
        }
    )
    for name in ("submit_resource_job", "poll_job", "collect_job", "cancel_job"):
        assert tm.exists(name)
