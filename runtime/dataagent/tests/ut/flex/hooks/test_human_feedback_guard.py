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
"""Unit tests for the final HITL guard planner post-hook."""

from __future__ import annotations

from typing import Any, cast

from langchain_core.messages import AIMessage, ToolMessage

from dataagent.core.flex.hooks.human_feedback_guard import (
    FINAL_HITL_GUARD_TRIGGERED_KEY,
    HUMAN_FEEDBACK_TOOL_NAME,
    human_feedback_guard,
)


def _complete_result(message: AIMessage | list[Any] | None = None) -> dict[str, Any]:
    return {
        "messages": message if message is not None else AIMessage(content="交付完成"),
        "complete": True,
    }


def _original_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "messages": [],
        "enable_human_feedback": True,
    }
    state.update(overrides)
    return state


def _run_guard(
    *,
    result: dict[str, Any] | None = None,
    original_state: dict[str, Any] | None = None,
    runtime: Any = None,
    conditions: str | list[Any] | None = None,
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        human_feedback_guard(
            cast(Any, result or _complete_result()),
            runtime,
            original_state=cast(Any, original_state if original_state is not None else _original_state()),
            human_feedback_guard_conditions=conditions,
        ),
    )


def _last_hitl_tool_call(message: Any) -> dict[str, Any]:
    assert isinstance(message, AIMessage)
    tool_call = cast(dict[str, Any], message.tool_calls[-1])
    assert tool_call["name"] == HUMAN_FEEDBACK_TOOL_NAME
    return tool_call


class RuntimeStub:
    def __init__(self) -> None:
        self.cache: dict[str, Any] = {}

    def get_all_config(self) -> dict[str, Any]:
        return {"AGENT_CONFIG": {"enable_human_feedback": True}}

    def get_cache(self, key: str, default: Any = None) -> Any:
        return self.cache.get(key, default)

    def set_cache(self, key: str, value: Any) -> None:
        self.cache[key] = value


def test_human_feedback_guard_injects_final_hitl_tool_call() -> None:
    result = _run_guard()

    message = result["messages"]
    assert isinstance(message, AIMessage)
    assert result["complete"] is False
    assert result["need_human_feedback"] is True
    assert result["__hitl_in_current_turn__"] is False
    assert result[FINAL_HITL_GUARD_TRIGGERED_KEY] is True
    assert _last_hitl_tool_call(message)["args"]["pending_action"] == ""
    assert message.content == "交付完成"


def test_human_feedback_guard_sets_pending_action_from_string() -> None:
    result = _run_guard(conditions=" 请检查最终报告是否可以交付 ")

    message = result["messages"]
    assert isinstance(message, AIMessage)
    assert _last_hitl_tool_call(message)["args"]["pending_action"] == "请检查最终报告是否可以交付"


def test_human_feedback_guard_sets_pending_action_from_list() -> None:
    result = _run_guard(conditions=[" 请确认数据口径 ", "", "如需调整，请说明修改点"])

    message = result["messages"]
    assert isinstance(message, AIMessage)
    assert _last_hitl_tool_call(message)["args"]["pending_action"] == "• 请确认数据口径  \n• 如需调整，请说明修改点"


def test_human_feedback_guard_still_injects_when_history_has_hitl() -> None:
    hitl_ai_message = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "tc_hitl_1",
                "name": HUMAN_FEEDBACK_TOOL_NAME,
                "args": {"reason": "确认", "pending_action": ""},
                "type": "tool_call",
            }
        ],
    )
    hitl_tool_message = ToolMessage(
        content="继续完善",
        tool_call_id="tc_hitl_1",
        name=HUMAN_FEEDBACK_TOOL_NAME,
    )

    result = _run_guard(original_state=_original_state(messages=[hitl_ai_message, hitl_tool_message]))

    assert result["complete"] is False
    assert _last_hitl_tool_call(result["messages"])["name"] == HUMAN_FEEDBACK_TOOL_NAME


def test_human_feedback_guard_skips_after_final_guard_triggered() -> None:
    complete_result = _complete_result()

    result = _run_guard(
        result=complete_result,
        original_state=_original_state(**{FINAL_HITL_GUARD_TRIGGERED_KEY: True}),
    )

    assert result is complete_result


def test_human_feedback_guard_updates_last_ai_message_in_list() -> None:
    result = _run_guard(
        result=_complete_result([ToolMessage(content="工具结果", tool_call_id="tc_1"), AIMessage(content="完成")])
    )

    messages = result["messages"]
    assert isinstance(messages, list)
    assert _last_hitl_tool_call(messages[-1])["name"] == HUMAN_FEEDBACK_TOOL_NAME


def test_human_feedback_guard_uses_runtime_fallback_without_original_state() -> None:
    runtime = RuntimeStub()
    first_result = cast(dict[str, Any], human_feedback_guard(cast(Any, _complete_result()), cast(Any, runtime)))
    second_input = _complete_result(AIMessage(content="确认后结束"))
    second_result = human_feedback_guard(cast(Any, second_input), cast(Any, runtime))

    assert first_result["complete"] is False
    assert _last_hitl_tool_call(first_result["messages"])["name"] == HUMAN_FEEDBACK_TOOL_NAME
    assert second_result is second_input
