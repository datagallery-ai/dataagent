# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Unit tests for HumanFeedbackNode empty-feedback retry + sentinel behavior."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from dataagent.core.flex.nodes.human_feedback import (
    MAX_EMPTY_FEEDBACK_RETRIES,
    HumanFeedbackNode,
)


def _build_state(last_message: Any) -> Any:
    """Build a minimal FlexState dict for terminal_mode HITL testing."""
    return {
        "messages": [last_message],
        "terminal_mode": True,
        "need_human_feedback": True,
        "enable_human_feedback": True,
        "feedback": "",
        "hitl_count": 0,
        "user_id": "test_user",
        "session_id": "test_session",
        "run_id": 0,
        "sub_id": 0,
    }


def _feedback_ai_message(reason: str = "请确认", pending_action: str = "创建实验") -> AIMessage:
    """Build an AIMessage carrying a request_human_feedback tool call."""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": "tc_hitl_1",
                "name": "request_human_feedback",
                "args": {"reason": reason, "pending_action": pending_action},
                "type": "tool_call",
            }
        ],
        invalid_tool_calls=[],
    )


def _patch_framework_deps(monkeypatch, input_side_effects: list[str]) -> MagicMock:
    """Patch stream writer / renderer / input for terminal_mode path.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        input_side_effects: sequence of strings returned by input() in order.

    Returns:
        The mock writer (so tests can assert on emitted messages).
    """
    import dataagent.core.flex.nodes.human_feedback as mod

    writer = MagicMock()
    monkeypatch.setattr(mod, "get_stream_writer", lambda: writer)
    monkeypatch.setattr(mod, "render_active_human_feedback_prompt", lambda **_: False)
    monkeypatch.setattr(mod, "suspend_active_renderer", lambda: None)
    monkeypatch.setattr(mod, "resume_active_renderer", lambda: None)
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=list(input_side_effects)))
    return writer


def _tool_message(result: Any) -> ToolMessage:
    """Extract the ToolMessage from the updated_state returned by _aprocess."""
    msgs = result.get("messages", [])
    tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1, f"expected exactly 1 ToolMessage, got {len(tool_msgs)}"
    return tool_msgs[0]


@pytest.mark.asyncio
async def test_empty_feedback_retries_then_sentinel(monkeypatch):
    """All-empty input → retried MAX_EMPTY_FEEDBACK_RETRIES times → sentinel ToolMessage."""
    inputs = ["", "   ", "\n\t"]
    assert len(inputs) == MAX_EMPTY_FEEDBACK_RETRIES
    _patch_framework_deps(monkeypatch, inputs)

    node = HumanFeedbackNode("human_feedback")
    state = _build_state(_feedback_ai_message())

    result = cast(dict[str, Any], await node._aprocess(state, runtime=None))

    tm = _tool_message(result)
    assert "[SYSTEM]" in tm.content
    assert "未提供有效反馈" in tm.content
    assert "禁止" in tm.content
    # Sentinel must instruct planner to report already-completed actions truthfully,
    # preventing the planner from "forgetting" completed work (see issue #6 e2e analysis).
    assert "如实汇报已完成的操作结果" in tm.content
    # "禁止" must be scoped to NEW actions only, not all actions.
    assert "新的自主决策" in tm.content
    assert result["need_human_feedback"] is False
    assert result["__hitl_in_current_turn__"] is True


@pytest.mark.asyncio
async def test_valid_feedback_on_first_try_no_retry(monkeypatch):
    """Non-empty first input → no retry, ToolMessage carries the user's text."""
    _patch_framework_deps(monkeypatch, ["确认创建"])

    node = HumanFeedbackNode("human_feedback")
    state = _build_state(_feedback_ai_message())

    result = await node._aprocess(state, runtime=None)

    tm = _tool_message(result)
    assert tm.content == "确认创建"
    assert "[SYSTEM]" not in tm.content


@pytest.mark.asyncio
async def test_empty_then_valid_feedback_retries_then_succeeds(monkeypatch):
    """Empty first, valid second → retried once, ToolMessage carries the valid text."""
    _patch_framework_deps(monkeypatch, ["", "选 XBB.1 样本 904012"])

    node = HumanFeedbackNode("human_feedback")
    state = _build_state(_feedback_ai_message())

    result = await node._aprocess(state, runtime=None)

    tm = _tool_message(result)
    assert tm.content == "选 XBB.1 样本 904012"
    assert "[SYSTEM]" not in tm.content


@pytest.mark.asyncio
async def test_no_request_human_feedback_tool_call_returns_early(monkeypatch):
    """AIMessage without request_human_feedback → early return, no input() call."""
    mock_input = MagicMock()
    monkeypatch.setattr("builtins.input", mock_input)
    import dataagent.core.flex.nodes.human_feedback as mod

    monkeypatch.setattr(mod, "get_stream_writer", lambda: MagicMock())

    node = HumanFeedbackNode("human_feedback")
    ai_msg = AIMessage(content="no tool call here", tool_calls=[], invalid_tool_calls=[])
    state = _build_state(ai_msg)

    result = cast(dict[str, Any], await node._aprocess(state, runtime=None))

    assert result["need_human_feedback"] is False
    assert result["__hitl_processed__"] is True
    mock_input.assert_not_called()


@pytest.mark.asyncio
async def test_sentinel_includes_attempt_count(monkeypatch):
    """Sentinel message reports the number of empty attempts."""
    _patch_framework_deps(monkeypatch, ["", "", ""])

    node = HumanFeedbackNode("human_feedback")
    state = _build_state(_feedback_ai_message())

    result = await node._aprocess(state, runtime=None)

    tm = _tool_message(result)
    assert str(MAX_EMPTY_FEEDBACK_RETRIES) in tm.content
