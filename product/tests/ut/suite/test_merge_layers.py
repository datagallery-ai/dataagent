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
"""Tests for ``merge_layers``."""

from dataagent.core.suite.merge import (
    apply_override_keys,
    extract_user_layer,
    merge_layers,
    parse_override_keys,
)
from dataagent.core.suite.validation import validate_strict_duplicates


def test_merge_layers_list_append_high_priority_first() -> None:
    default = {"TOOLS": {"local_functions": [{"function": "read_file"}]}}
    suite = {"TOOLS": {"local_functions": [{"function": "sql_tool"}]}}
    user = {"TOOLS": {"local_functions": [{"function": "user_tool"}]}}
    result = merge_layers([default, suite, user])
    names = [item["function"] for item in result["TOOLS"]["local_functions"]]
    assert names == ["user_tool", "sql_tool", "read_file"]


def test_merge_layers_workflow_merge_by_node() -> None:
    default = {"ACTOR_LOOP": [{"node": "planner", "module": "a", "x": 1}]}
    user = {"ACTOR_LOOP": [{"node": "planner", "x": 2, "y": 3}]}
    result = merge_layers([default, user])
    planner = result["ACTOR_LOOP"][0]
    assert planner["x"] == 2
    assert planner["y"] == 3
    assert planner["module"] == "a"


def test_extract_user_layer_only_user_keys() -> None:
    interpolated = {"A": 1, "B": 2, "C": 3}
    user_config = {"A": 1, "C": 3}
    layer = extract_user_layer(interpolated, user_config)
    assert layer == {"A": 1, "C": 3}


def test_extract_user_layer_partial_hooks_excludes_default_siblings() -> None:
    """User layer must not include default hook siblings when user only overrides one slot."""
    interpolated = {
        "HOOKS": {
            "nodes": {
                "planner": {"pre": ["pruner", "portraiter"]},
                "executor": {"post": ["user_hook"]},
            }
        }
    }
    user_config = {"HOOKS": {"nodes": {"executor": {"post": ["user_hook"]}}}}
    layer = extract_user_layer(interpolated, user_config)
    assert layer == {"HOOKS": {"nodes": {"executor": {"post": ["user_hook"]}}}}
    assert "planner" not in layer["HOOKS"]["nodes"]


def test_extract_user_layer_empty_list_stays_empty() -> None:
    """User-written empty list must not promote interpolated default list items."""
    interpolated = {"HOOKS": {"nodes": {"executor": {"post": ["default_hook"]}}}}
    user_config = {"HOOKS": {"nodes": {"executor": {"post": []}}}}
    layer = extract_user_layer(interpolated, user_config)
    assert layer["HOOKS"]["nodes"]["executor"]["post"] == []


def test_extract_user_layer_partial_workspace_keeps_only_user_fields() -> None:
    """User ``WORKSPACE.path`` alone must not pull default ``allow_path`` into the user layer."""
    interpolated = {
        "WORKSPACE": {
            "path": "/tmp/user-ws",
            "allow_path": ["/tmp/default-allow"],
        }
    }
    user_config = {"WORKSPACE": {"path": "/tmp/user-ws"}}
    layer = extract_user_layer(interpolated, user_config)
    assert layer == {"WORKSPACE": {"path": "/tmp/user-ws"}}


def test_merge_layers_prompt_template_system_append_order() -> None:
    """Merged planner ``prompt_template.system`` lists follow user → suite → default order."""
    default = {
        "ACTOR_LOOP": [
            {
                "node": "planner",
                "module": "default.planner",
                "prompt_template": {"system": [{"content": "default"}]},
            }
        ]
    }
    suite = {
        "ACTOR_LOOP": [
            {
                "node": "planner",
                "prompt_template": {"system": [{"path": "/suite/system.md"}]},
            }
        ]
    }
    user = {
        "ACTOR_LOOP": [
            {
                "node": "planner",
                "prompt_template": {"system": [{"content": "user"}]},
            }
        ]
    }
    result = merge_layers([default, suite, user])
    specs = result["ACTOR_LOOP"][0]["prompt_template"]["system"]
    assert specs[0] == {"content": "user"}
    assert specs[1] == {"path": "/suite/system.md"}
    assert specs[2] == {"content": "default"}


def test_merge_layers_same_priority_suite_name_order() -> None:
    """Same-priority suite layers merged with smaller ``name`` hook entries first (a before z)."""
    default = {"HOOKS": {"nodes": {"planner": {"pre": ["pruner"]}}}}
    alpha_suite = {"HOOKS": {"nodes": {"planner": {"pre": ["alpha_hook"]}}}}
    beta_suite = {"HOOKS": {"nodes": {"planner": {"pre": ["beta_hook"]}}}}
    # merge_layers order: default → beta (higher layer) → alpha (highest suite layer)
    result = merge_layers([default, beta_suite, alpha_suite])
    specs = result["HOOKS"]["nodes"]["planner"]["pre"]
    assert specs == ["alpha_hook", "beta_hook", "pruner"]


def test_merge_layers_priority_override_suite_order() -> None:
    """Higher-priority Suite HOOKS entries appear before lower-priority Suite entries."""
    default = {"HOOKS": {"nodes": {"planner": {"pre": ["pruner"]}}}}
    low_suite = {"HOOKS": {"nodes": {"planner": {"pre": ["low_hook"]}}}}
    high_suite = {"HOOKS": {"nodes": {"planner": {"pre": ["high_hook"]}}}}
    result = merge_layers([default, low_suite, high_suite])
    specs = result["HOOKS"]["nodes"]["planner"]["pre"]
    assert specs == ["high_hook", "low_hook", "pruner"]


def test_validate_strict_duplicates_tools() -> None:
    config = {
        "TOOLS": {
            "local_functions": [
                {"function": "a"},
                {"function": "a"},
            ]
        }
    }
    try:
        validate_strict_duplicates(config)
        raised = False
    except ValueError as exc:
        raised = True
        assert "Duplicate" in str(exc)
    assert raised


def test_apply_override_keys_replaces_actor_loop() -> None:
    """``OVERRIDE_KEYS`` replaces a top-level section instead of workflow structural merge."""
    default = {
        "ACTOR_LOOP": [
            {"node": "planner", "module": "default.planner"},
            {"node": "executor", "module": "default.executor", "max_tool_result_length": 8192},
        ]
    }
    user_layer = {
        "ACTOR_LOOP": [
            {"node": "nl2sql_react", "module": "user.planner", "chat_model": {"name": "qwen3"}},
            {"node": "executor", "module": "user.executor", "max_concurrency": 5},
        ]
    }
    result = merge_layers([default, user_layer])
    assert [item["node"] for item in result["ACTOR_LOOP"]] == ["planner", "executor", "nl2sql_react"]

    apply_override_keys(result, user_layer, frozenset({"ACTOR_LOOP"}))
    nodes = result["ACTOR_LOOP"]
    assert [item["node"] for item in nodes] == ["nl2sql_react", "executor"]
    assert nodes[1]["max_concurrency"] == 5
    assert "max_tool_result_length" not in nodes[1]


def test_parse_override_keys_rejects_non_list() -> None:
    """Invalid ``OVERRIDE_KEYS`` shape raises before merge."""
    try:
        parse_override_keys({"OVERRIDE_KEYS": "ACTOR_LOOP"})
        raised = False
    except ValueError as exc:
        raised = True
        assert "OVERRIDE_KEYS" in str(exc)
    assert raised


def test_merge_configs_honors_override_keys() -> None:
    """Two-layer ``merge_configs`` applies ``OVERRIDE_KEYS`` on the override mapping."""
    from dataagent.config.config_manager import ConfigManager

    default = {"ACTOR_LOOP": [{"node": "planner", "module": "a"}]}
    override = {
        "OVERRIDE_KEYS": ["ACTOR_LOOP"],
        "ACTOR_LOOP": [{"node": "nl2sql_react", "module": "b"}],
    }
    result = ConfigManager.merge_configs(default, override)
    assert [item["node"] for item in result["ACTOR_LOOP"]] == ["nl2sql_react"]
    assert "OVERRIDE_KEYS" not in result
