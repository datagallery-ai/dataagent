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
"""Tests for post-merge Suite validation."""

from __future__ import annotations

import pytest

from dataagent.core.suite.validation import validate_merged_config, validate_strict_duplicates
from dataagent.utils.runtime_paths import dataagent_package_path


def test_validate_rejects_explicit_sub_agent_tool() -> None:
    config = {
        "TOOLS": {
            "local_functions": [
                {"function": "sub_agent_tool"},
            ]
        }
    }
    with pytest.raises(ValueError, match="SUBAGENT_CONFIGS"):
        validate_merged_config(config)


def test_validate_rejects_duplicate_hook_specs() -> None:
    config = {
        "HOOKS": {
            "nodes": {
                "planner": {
                    "pre": ["pruner", "pruner"],
                }
            }
        }
    }
    with pytest.raises(ValueError, match="Duplicate"):
        validate_strict_duplicates(config)


def test_validate_accepts_example_suite_subagent_path_with_user_allow_path() -> None:
    subagent = dataagent_package_path(
        "core",
        "suite",
        "builtin_suites",
        "example_suite",
        "subagents",
        "arithmetic_ref.yaml",
    )
    config = {
        "SUBAGENT_CONFIGS": [{"path": str(subagent)}],
        "WORKSPACE": {
            "allow_path": [str(dataagent_package_path("core", "suite", "builtin_suites"))],
        },
    }
    validate_merged_config(config)


def test_validate_merged_config_accepts_governance_config() -> None:
    validate_merged_config(
        {
            "GOVERNANCE": {
                "invisibility": ["submit_subagent"],
                "policies": [
                    {
                        "id": "data_analysis_orchestration",
                        "applies_to": ["submit_subagent"],
                        "priority": 45,
                        "address": "tests.ut.tools.test_tool_hooks.governance_policy_allow",
                    }
                ],
                "argument_injectors": [
                    {
                        "id": "inject_runtime_context",
                        "applies_to": ["some_tool"],
                        "priority": 50,
                        "address": "tests.ut.tools.test_tool_hooks.governance_inject_hidden",
                    }
                ],
            }
        }
    )


@pytest.mark.parametrize(
    ("governance", "match"),
    [
        ([], "GOVERNANCE must be a mapping"),
        ({"invisibility": "submit_subagent"}, "GOVERNANCE.invisibility must be a list"),
        ({"invisibility": [1]}, "GOVERNANCE.invisibility entries must be non-empty strings"),
        ({"policies": [{"id": "p", "applies_to": ["submit_subagent"]}]}, "address"),
        ({"policies": [{"id": "p", "applies_to": ["*"], "address": "x.y"}]}, "does not allow '*'"),
        (
            {
                "argument_injectors": [
                    {"id": "inject", "applies_to": ["tool_a"], "address": "x.y"},
                    {"id": "inject", "applies_to": ["tool_b"], "address": "x.z"},
                ]
            },
            "Duplicate GOVERNANCE.argument_injectors id",
        ),
    ],
)
def test_validate_merged_config_rejects_invalid_governance_config(governance: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        validate_merged_config({"GOVERNANCE": governance})
