# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
from dataagent.core.flex.utils.hitl_config import (
    append_human_feedback_conditions_to_instructions,
    format_human_feedback_conditions_block,
    is_human_feedback_enabled,
    normalize_human_feedback_conditions,
    resolve_human_feedback_conditions,
    resolve_scenario_instructions,
)
from dataagent.utils.constants import HUMAN_FEEDBACK_CONDITION_ACTION_SUFFIX


def test_normalize_human_feedback_conditions_list():
    assert normalize_human_feedback_conditions(["  a  ", "", "b"]) == ["a", "b"]


def test_normalize_human_feedback_conditions_string():
    assert normalize_human_feedback_conditions("调用任何工具之前, 或者生成报告之前") == [
        "调用任何工具之前, 或者生成报告之前"
    ]


def test_resolve_human_feedback_conditions_from_scenario():
    config = {
        "SCENARIO": {
            "chat": {
                "human_feedback_conditions": ["调用任何工具之前"],
            }
        }
    }
    assert resolve_human_feedback_conditions(config, mode="chat") == ["调用任何工具之前"]


def test_format_human_feedback_conditions_block():
    block = format_human_feedback_conditions_block(
        ["调用read_file工具之前", "调用perceive_metadata_from_memory工具之前"]
    )
    assert block == "\n".join(
        [
            f"调用read_file工具之前，{HUMAN_FEEDBACK_CONDITION_ACTION_SUFFIX}",
            f"调用perceive_metadata_from_memory工具之前，{HUMAN_FEEDBACK_CONDITION_ACTION_SUFFIX}",
        ]
    )


def test_append_human_feedback_conditions_to_instructions():
    merged = append_human_feedback_conditions_to_instructions(
        "先获取表结构信息。",
        ["调用perceive_metadata_from_memory工具之前"],
    )
    assert merged.startswith("先获取表结构信息。")
    assert f"调用perceive_metadata_from_memory工具之前，{HUMAN_FEEDBACK_CONDITION_ACTION_SUFFIX}" in merged


def test_resolve_scenario_instructions_appends_conditions_when_hitl_enabled():
    config = {
        "AGENT_CONFIG": {"enable_human_feedback": True},
        "SCENARIO": {
            "chat": {
                "instructions": "生成分析报告。",
                "human_feedback_conditions": ["调用perceive_metadata_from_memory工具之前"],
            }
        },
    }
    instructions = resolve_scenario_instructions(config, mode="chat")
    assert instructions.startswith("生成分析报告。")
    assert f"调用perceive_metadata_from_memory工具之前，{HUMAN_FEEDBACK_CONDITION_ACTION_SUFFIX}" in instructions


def test_resolve_scenario_instructions_ignores_conditions_when_hitl_disabled():
    config = {
        "AGENT_CONFIG": {"enable_human_feedback": False},
        "SCENARIO": {
            "chat": {
                "instructions": "生成分析报告。",
                "human_feedback_conditions": ["调用任何工具之前"],
            }
        },
    }
    assert resolve_scenario_instructions(config, mode="chat") == "生成分析报告。"


def test_is_human_feedback_enabled_legacy_flag():
    config = {"AGENT_CONFIG": {"enable_human_feedback": True}}
    assert is_human_feedback_enabled(config) is True
