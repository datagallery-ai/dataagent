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
"""v3.4 方案3回归：LLM 网关瞬时故障（500 等）不终止任务——complete=False 让
actor loop 重回 planner 重试；连续失败达 MAX_CONSECUTIVE_LLM_ERRORS 才放弃。"""

from __future__ import annotations

from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.core.flex.nodes.planner import (
    MAX_CONSECUTIVE_LLM_ERRORS,
    Planner,
    _count_trailing_error_messages,
)
from dataagent.core.managers.llm_manager.llm_client import LLMCallError, LLMErrorCategory


def _make_planner() -> Planner:
    planner = Planner.__new__(Planner)
    planner.name = "planner"
    return planner


def _llm_error() -> LLMCallError:
    return LLMCallError(LLMErrorCategory.SERVER_ERROR, "gateway 500", status_code=500)


class TestCountTrailingErrorMessages:
    def test_empty_messages(self):
        assert _count_trailing_error_messages([]) == 0
        assert _count_trailing_error_messages(None) == 0

    def test_trailing_error_messages_counted(self):
        msgs = [
            HumanMessage(content="hi"),
            AIMessage(content="e1", additional_kwargs={"error": True}),
            AIMessage(content="e2", additional_kwargs={"error": True}),
        ]
        assert _count_trailing_error_messages(msgs) == 2

    def test_stops_at_non_error_message(self):
        msgs = [
            AIMessage(content="e1", additional_kwargs={"error": True}),
            ToolMessage(content="ok", tool_call_id="t1"),
            AIMessage(content="e2", additional_kwargs={"error": True}),
        ]
        assert _count_trailing_error_messages(msgs) == 1

    def test_no_error_messages(self):
        msgs = [HumanMessage(content="hi"), AIMessage(content="ok")]
        assert _count_trailing_error_messages(msgs) == 0


class TestBuildErrorResultRetry:
    def test_first_llm_error_returns_retry_not_complete(self):
        """首次网关瞬时故障 complete=False，任务不终止、curr_iter 前进。"""
        planner = _make_planner()
        writer = MagicMock()
        state = {"messages": [HumanMessage(content="task")], "curr_iter": 5}
        result = planner._build_error_result(writer, _llm_error(), terminal_mode=False, state=state)
        assert result["complete"] is False
        assert result["curr_iter"] == 6
        msg = result["messages"]
        assert msg.additional_kwargs.get("error") is True
        assert "[SYSTEM]" in msg.content
        assert "瞬时" in msg.content

    def test_second_llm_error_still_retries(self):
        planner = _make_planner()
        writer = MagicMock()
        state = {
            "messages": [
                HumanMessage(content="task"),
                AIMessage(content="e1", additional_kwargs={"error": True}),
            ],
            "curr_iter": 6,
        }
        result = planner._build_error_result(writer, _llm_error(), terminal_mode=False, state=state)
        assert result["complete"] is False

    def test_consecutive_errors_reach_cap_gives_up(self):
        """连续失败达上限（MAX_CONSECUTIVE_LLM_ERRORS）后放弃，防无限循环。"""
        planner = _make_planner()
        writer = MagicMock()
        state = {
            "messages": [
                HumanMessage(content="task"),
                AIMessage(content="e1", additional_kwargs={"error": True}),
                AIMessage(content="e2", additional_kwargs={"error": True}),
            ],
            "curr_iter": 7,
        }
        assert MAX_CONSECUTIVE_LLM_ERRORS == 3
        result = planner._build_error_result(writer, _llm_error(), terminal_mode=False, state=state)
        assert result["complete"] is True
        assert "error" in result

    def test_success_message_resets_consecutive_count(self):
        """中间穿插成功消息后计数归零，重新允许重试。"""
        planner = _make_planner()
        writer = MagicMock()
        state = {
            "messages": [
                AIMessage(content="e1", additional_kwargs={"error": True}),
                AIMessage(content="e2", additional_kwargs={"error": True}),
                AIMessage(content="recovered"),  # 成功消息隔断
                ToolMessage(content="ok", tool_call_id="t1"),
                AIMessage(content="e3", additional_kwargs={"error": True}),
            ],
            "curr_iter": 9,
        }
        result = planner._build_error_result(writer, _llm_error(), terminal_mode=False, state=state)
        assert result["complete"] is False

    def test_non_llm_error_gives_up_immediately(self):
        """非 LLMCallError（代码 bug 类）保持原行为：立即终止。"""
        planner = _make_planner()
        writer = MagicMock()
        state = {"messages": [], "curr_iter": 1}
        result = planner._build_error_result(writer, ValueError("bug"), terminal_mode=False, state=state)
        assert result["complete"] is True

    def test_no_state_gives_up(self):
        """调用方未传 state（旧行为兼容）不重试。"""
        planner = _make_planner()
        writer = MagicMock()
        result = planner._build_error_result(writer, _llm_error(), terminal_mode=False, state=None)
        assert result["complete"] is True

    def test_terminal_mode_emits_error_event(self):
        planner = _make_planner()
        writer = MagicMock()
        state = {"messages": [], "curr_iter": 1}
        planner._build_error_result(writer, _llm_error(), terminal_mode=True, state=state)
        calls = [c.args[0] for c in writer.call_args_list if c.args]
        assert any(isinstance(c, dict) and c.get("type") == "planner_error" for c in calls)
