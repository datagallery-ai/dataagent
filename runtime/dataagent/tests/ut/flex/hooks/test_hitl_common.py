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
"""通用 HITL 基座（hitl_common）单元测试：门控/预算/去重/载荷提取/消息注入/状态契约。

场景行为（golden 匹配、action 分类口径、程序化答复、泄漏防线）的测试在
``tests/ut/suite/test_de_agent_hitl_auto_resolver.py``。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.core.flex.hooks.hitl_common import (
    DEFAULT_HITL_BUDGET,
    build_hook_return,
    extract_hil_question,
    extract_original_query,
    find_human_feedback_call,
    get_hitl_budget,
    get_hitl_question_history,
    get_hitl_turn_count,
    inject_fallback_reply,
    inject_simple_reply,
    inject_tool_message,
    is_auto_resolve_enabled,
    normalize_question,
    runtime_auto_resolve_enabled,
    update_action_node,
)


class MockLLM:
    """Mock LLM that returns a fixed response."""

    def __init__(self, response: str = "这是自动回复内容"):
        self.response = response

    def invoke(self, messages: list) -> MagicMock:
        mock = MagicMock()
        mock.content = self.response
        return mock


class RuntimeStub:
    """Minimal runtime stub for testing."""

    def __init__(
        self,
        auto_resolve: bool = True,
        hitl_enabled: bool = True,
    ):
        self._config: dict[str, Any] = {
            "AGENT_CONFIG": {
                "hitl_auto_resolve": auto_resolve,
                "enable_human_feedback": hitl_enabled,
            }
        }

    def get_all_config(self) -> dict[str, Any]:
        return self._config


def _make_hil_tool_call(
    call_id: str = "call_123", reason: str = "请确认", pending_action: str = "数据口径是否正确"
) -> dict:
    return {
        "id": call_id,
        "name": "request_human_feedback",
        "args": {"reason": reason, "pending_action": pending_action},
        "type": "tool_call",
    }


def _make_ai_message_with_hil(
    content: str = "完成", call_id: str = "call_123", *, pending_action: str = "数据口径是否正确"
) -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=[_make_hil_tool_call(call_id=call_id, pending_action=pending_action)],
    )


# ─────────────────────────────────────────────────────────────────────────────
# HITL 调用检测与载荷提取
# ─────────────────────────────────────────────────────────────────────────────


class TestFindHumanFeedbackCall:
    def test_finds_tool_call_in_aimessage(self):
        messages = [_make_ai_message_with_hil()]
        result = find_human_feedback_call(messages)
        assert result is not None
        assert result["name"] == "request_human_feedback"

    def test_returns_none_when_no_hil_call(self):
        messages = [AIMessage(content="hello")]
        assert find_human_feedback_call(messages) is None

    def test_returns_last_hil_call(self):
        messages = [
            AIMessage(content="", tool_calls=[_make_hil_tool_call(call_id="c1")]),
            AIMessage(content="", tool_calls=[_make_hil_tool_call(call_id="c2")]),
        ]
        result = find_human_feedback_call(messages)
        assert result is not None
        assert result["id"] == "c2"


class TestExtractOriginalQuery:
    def test_extracts_from_human_message(self):
        messages = [HumanMessage(content="<user_query>查询用户订单数</user_query>")]
        assert extract_original_query(messages) == "查询用户订单数"

    def test_returns_empty_for_empty_messages(self):
        assert extract_original_query([]) == ""

    def test_returns_empty_when_first_not_human(self):
        messages = [AIMessage(content="hello")]
        assert extract_original_query(messages) == ""


class TestExtractHilQuestion:
    def test_concatenates_reason_and_pending_action(self):
        call = _make_hil_tool_call(pending_action="请确认数据口径", reason="需要确认")
        assert extract_hil_question(call) == "需要确认\n请确认数据口径"

    def test_pending_action_only(self):
        call = _make_hil_tool_call(pending_action="请确认数据口径", reason="")
        assert extract_hil_question(call) == "请确认数据口径"

    def test_reason_only(self):
        call = _make_hil_tool_call(pending_action="", reason="需要确认")
        assert extract_hil_question(call) == "需要确认"

    def test_reason_substance_not_lost_when_pending_action_is_label(self):
        """q68 回归：实质内容在 reason、pending_action 只有短标签时，表名不得丢失。"""
        call = _make_hil_tool_call(
            reason="检索到候选表 agapdw.dwd_promot_client_bhv_passback_dm，倾向用它",
            pending_action="确认数据源表选择",
        )
        q = extract_hil_question(call)
        assert "dwd_promot_client_bhv_passback_dm" in q
        assert "确认数据源表选择" in q


class TestInjectToolMessage:
    def test_injects_tool_message_at_end(self):
        messages = [HumanMessage(content="hello"), AIMessage(content="world")]
        updated = inject_tool_message(messages, "call_123", "auto reply")
        assert len(updated) == 3
        assert isinstance(updated[-1], ToolMessage)
        assert updated[-1].content == "auto reply"
        assert updated[-1].tool_call_id == "call_123"


# ─────────────────────────────────────────────────────────────────────────────
# 启用门控
# ─────────────────────────────────────────────────────────────────────────────


class TestIsAutoResolveEnabled:
    def test_true_from_state(self):
        state = {"hitl_auto_resolve": True}
        assert is_auto_resolve_enabled(state, None, None) is True

    def test_false_from_state(self):
        state = {"hitl_auto_resolve": False}
        assert is_auto_resolve_enabled(state, None, None) is False

    def test_true_from_runtime(self):
        runtime = RuntimeStub(auto_resolve=True)
        assert is_auto_resolve_enabled({}, None, runtime) is True

    def test_false_from_runtime(self):
        runtime = RuntimeStub(auto_resolve=False)
        assert is_auto_resolve_enabled({}, None, runtime) is False

    def test_original_state_takes_priority(self):
        """original_state（pre-hook 恒 None，post-hook 为节点前快照）优先于 state。"""
        assert is_auto_resolve_enabled({"hitl_auto_resolve": False}, {"hitl_auto_resolve": True}, None) is True


class TestRuntimeAutoResolveEnabled:
    def test_none_runtime_returns_false(self):
        assert runtime_auto_resolve_enabled(None) is False

    def test_true_from_agent_config(self):
        assert runtime_auto_resolve_enabled(RuntimeStub(auto_resolve=True)) is True

    def test_false_when_flag_missing_or_false(self):
        assert runtime_auto_resolve_enabled(RuntimeStub(auto_resolve=False)) is False

    def test_runtime_without_get_all_config_returns_false(self):
        assert runtime_auto_resolve_enabled(object()) is False

    def test_get_all_config_failure_returns_false(self):
        class _BrokenRuntime:
            def get_all_config(self):
                raise RuntimeError("boom")

        assert runtime_auto_resolve_enabled(_BrokenRuntime()) is False


# ─────────────────────────────────────────────────────────────────────────────
# 交互预算与轮数
# ─────────────────────────────────────────────────────────────────────────────


class TestGetHitlBudget:
    def test_default_when_missing(self):
        assert DEFAULT_HITL_BUDGET == 4
        assert get_hitl_budget({}) == DEFAULT_HITL_BUDGET

    def test_reads_from_state(self):
        assert get_hitl_budget({"hitl_budget": 7}) == 7

    def test_original_state_takes_priority(self):
        assert get_hitl_budget({"hitl_budget": 1}, {"hitl_budget": 9}) == 9

    def test_none_budget_falls_back_to_default(self):
        assert get_hitl_budget({"hitl_budget": None}) == DEFAULT_HITL_BUDGET

    def test_invalid_budget_falls_back_to_default(self):
        assert get_hitl_budget({"hitl_budget": "abc"}) == DEFAULT_HITL_BUDGET

    def test_zero_budget_falls_back_to_default(self):
        """0 视为未设置（falsy），回落缺省而非把预算清零。"""
        assert get_hitl_budget({"hitl_budget": 0}) == DEFAULT_HITL_BUDGET

    def test_string_budget_is_coerced(self):
        assert get_hitl_budget({"hitl_budget": "5"}) == 5


class TestGetHitlTurnCount:
    def test_default_zero(self):
        assert get_hitl_turn_count({}) == 0

    def test_reads_hitl_count(self):
        assert get_hitl_turn_count({"hitl_count": 3}) == 3

    def test_original_state_takes_priority(self):
        assert get_hitl_turn_count({"hitl_count": 1}, {"hitl_count": 5}) == 5

    def test_invalid_or_none_count_is_zero(self):
        assert get_hitl_turn_count({"hitl_count": "abc"}) == 0
        assert get_hitl_turn_count({"hitl_count": None}) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 问题去重
# ─────────────────────────────────────────────────────────────────────────────


class TestNormalizeQuestion:
    def test_empty_and_none(self):
        assert normalize_question("") == ""
        assert normalize_question(None) == ""

    def test_strips_punctuation_and_whitespace(self):
        assert normalize_question("源表 选择，对吗？") == "源表选择对吗"

    def test_lowercases_ascii(self):
        assert normalize_question("User ID?") == "userid"

    def test_full_width_punctuation_stripped(self):
        assert normalize_question("（确认）：数据源；") == "确认数据源"


class TestGetHitlQuestionHistory:
    def _answered_thread(self, call_id: str = "call_123") -> list:
        return [
            _make_ai_message_with_hil(call_id=call_id),
            ToolMessage(content="已答复", name="request_human_feedback", tool_call_id=call_id),
        ]

    def test_pairs_question_with_answer(self):
        history = get_hitl_question_history({}, messages=self._answered_thread())
        assert len(history) == 1
        assert history[0]["question"] == "请确认\n数据口径是否正确"
        assert history[0]["question_norm"] == "请确认数据口径是否正确"
        assert history[0]["answer"] == "已答复"

    def test_unmatched_tool_message_ignored(self):
        messages = [
            _make_ai_message_with_hil(call_id="c1"),
            ToolMessage(content="孤儿答复", name="request_human_feedback", tool_call_id="nope"),
        ]
        assert get_hitl_question_history({}, messages=messages) == []

    def test_empty_messages(self):
        assert get_hitl_question_history({}, messages=[]) == []

    def test_messages_param_preferred_over_state(self):
        state = {"messages": []}
        assert len(get_hitl_question_history(state, messages=self._answered_thread())) == 1

    def test_state_messages_used_when_arg_none(self):
        state = {"messages": self._answered_thread()}
        assert len(get_hitl_question_history(state)) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 消息注入与状态契约
# ─────────────────────────────────────────────────────────────────────────────


class TestInjectSimpleReply:
    def _state(self) -> dict[str, Any]:
        return {"messages": [], "complete": False, "need_human_feedback": True, "custom": 3}

    def test_returns_resolved_state_contract(self):
        result = inject_simple_reply(
            self._state(), [HumanMessage(content="q")], _make_hil_tool_call(), "拒绝该问题"
        )
        assert result["hitl_auto_resolved"] is True
        assert result["need_human_feedback"] is False
        assert isinstance(result["messages"][-1], ToolMessage)
        assert result["messages"][-1].content == "拒绝该问题"
        assert result["messages"][-1].tool_call_id == "call_123"

    def test_resolved_info_fields(self):
        result = inject_simple_reply(
            self._state(),
            [HumanMessage(content="q")],
            _make_hil_tool_call(),
            "拒绝该问题",
            action="UNA",
            reason="inappropriate",
        )
        info = result["hitl_resolved_info"]
        assert info["question"] == "(simple)"
        assert info["answer"] == "拒绝该问题"
        assert info["action"] == "UNA"
        assert info["reason"] == "inappropriate"
        assert info["task_id"] is None
        assert info["golden_sql_path"] == ""

    def test_preserves_state_extras(self):
        result = inject_simple_reply(
            self._state(), [HumanMessage(content="q")], _make_hil_tool_call(), "拒绝该问题"
        )
        assert result["custom"] == 3
        assert result["complete"] is False


class TestInjectFallbackReply:
    def _state(self) -> dict[str, Any]:
        return {"messages": [], "need_human_feedback": True}

    def test_injects_fallback_answer(self):
        result = inject_fallback_reply(
            self._state(), [HumanMessage(content="q")], _make_hil_tool_call(), reason="golden_not_found"
        )
        assert result["hitl_auto_resolved"] is True
        assert result["need_human_feedback"] is False
        assert "自主判断" in result["messages"][-1].content
        info = result["hitl_resolved_info"]
        assert info["question"] == "(fallback)"
        assert info["fallback_reason"] == "golden_not_found"
        assert info["golden_sql_path"] == ""


class TestBuildHookReturn:
    def test_plain_state_merge(self):
        state = {"messages": ["old"], "complete": True, "need_human_feedback": True, "custom": 3}
        updated = [HumanMessage(content="new")]
        info = {"action": "UNA"}
        result = build_hook_return(state, updated, info)
        assert result["messages"] == updated
        assert result["hitl_auto_resolved"] is True
        assert result["hitl_resolved_info"] is info
        assert result["need_human_feedback"] is False
        assert result["complete"] is True
        assert result["custom"] == 3


class TestUpdateActionNode:
    def test_does_not_raise_without_context(self):
        """无 context 环境时静默失败（异常吞掉），不得向 hook 调用方抛出。"""
        assert update_action_node({}, "call_x", "答复") is None
