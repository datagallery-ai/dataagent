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
"""Tests for plan_enforcer planner pre-hook (skill + tool-call dual triggers)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.core.cbb.base_agent import BaseAgent
from dataagent.core.flex.hooks.plan_enforcer import (
    PLAN_REQUIRED_THRESHOLD_KEY,
    SKILL_MD_READ_WITHOUT_PLAN_KEY,
    TOOL_CALL_COUNT_KEY,
    _count_tool_messages,
    _extract_read_skill_names,
    plan_enforcer,
)
from dataagent.core.flex.hooks.registry import resolve_builtin_hook

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ai_read_skill_md(skill_name: str, *, tc_id: str = "tc-1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "read_file", "args": {"path": f"skill/{skill_name}/SKILL.md"}, "id": tc_id}],
    )


def _ai_other_tool(name: str = "bash") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": {"cmd": "ls"}, "id": "tc-other"}])


def _tool_msg(tc_id: str = "tc-1") -> ToolMessage:
    return ToolMessage(content="r", tool_call_id=tc_id)


def _context_with_plan() -> SimpleNamespace:
    plan = SimpleNamespace(introduction="p", approach="a", todos=[])
    return SimpleNamespace(todolist_manager=SimpleNamespace(todolist=plan))


def _context_no_plan() -> SimpleNamespace:
    return SimpleNamespace(todolist_manager=SimpleNamespace(todolist=None))


def _runtime(*, pending: bool = False) -> SimpleNamespace:
    """构造伪 runtime。

    ``pending`` 对应 ``flex_planner_user_sync_pending``：True=当前 query 首轮迭代
    （user 消息尚未 sync），False=后续迭代（user 消息已入 state）。
    """
    return SimpleNamespace(tool_manager=None, env=SimpleNamespace(), flex_planner_user_sync_pending=pending)


@pytest.fixture
def patch_context_lookup(monkeypatch):
    def _patch(ctx: Any):
        import dataagent.core.flex.hooks.plan_enforcer as mod

        monkeypatch.setattr(mod, "get_context_for_flex_state", lambda state, runtime, *, swallow_errors=False: ctx)

    return _patch


# ---------------------------------------------------------------------------
# _extract_read_skill_names / _count_tool_messages
# ---------------------------------------------------------------------------


def test_extract_names_from_path():
    assert _extract_read_skill_names([_ai_read_skill_md("exp"), _tool_msg()]) == ["exp"]


def test_extract_names_dedup_preserve_order():
    msgs = [_ai_read_skill_md("a"), _ai_read_skill_md("b"), _ai_read_skill_md("a")]
    assert _extract_read_skill_names(msgs) == ["a", "b"]


def test_extract_names_ignores_non_skill_md():
    ai = AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": "skill/foo/README.md"}, "id": "x"}])
    assert _extract_read_skill_names([ai]) == []


def test_extract_names_handles_non_dict():
    fake = SimpleNamespace(tool_calls=[("read_file", {"path": "skill/x/SKILL.md"})])
    assert _extract_read_skill_names([fake]) == []


def test_count_tool_messages():
    msgs = [AIMessage(content="a"), _tool_msg("1"), _tool_msg("2"), _tool_msg("3")]
    assert _count_tool_messages(msgs) == 3


def test_count_tool_messages_empty():
    assert _count_tool_messages([]) == 0


# ---------------------------------------------------------------------------
# skill trigger
# ---------------------------------------------------------------------------


def test_skill_trigger_sets_true_when_configured_skill_read(patch_context_lookup):
    patch_context_lookup(_context_no_plan())
    state = {"messages": [_ai_read_skill_md("create-exp"), _tool_msg()]}
    out = plan_enforcer(state, _runtime(), require_plan_skills=["create-exp"])  # type: ignore[arg-type]
    assert out[SKILL_MD_READ_WITHOUT_PLAN_KEY] is True


def test_skill_trigger_false_when_read_skill_not_in_config(patch_context_lookup):
    patch_context_lookup(_context_no_plan())
    state = {"messages": [_ai_read_skill_md("simple"), _tool_msg()]}
    out = plan_enforcer(state, _runtime(), require_plan_skills=["create-exp"])  # type: ignore[arg-type]
    assert out[SKILL_MD_READ_WITHOUT_PLAN_KEY] is False


def test_skill_trigger_false_when_plan_exists(patch_context_lookup):
    patch_context_lookup(_context_with_plan())
    state = {"messages": [_ai_read_skill_md("create-exp"), _tool_msg()]}
    out = plan_enforcer(state, _runtime(), require_plan_skills=["create-exp"])  # type: ignore[arg-type]
    assert out[SKILL_MD_READ_WITHOUT_PLAN_KEY] is False


def test_skill_trigger_clears_stale_when_plan_now_exists(patch_context_lookup):
    patch_context_lookup(_context_with_plan())
    state = {"messages": [_ai_read_skill_md("create-exp")], SKILL_MD_READ_WITHOUT_PLAN_KEY: True}
    out = plan_enforcer(state, _runtime(), require_plan_skills=["create-exp"])  # type: ignore[arg-type]
    assert out[SKILL_MD_READ_WITHOUT_PLAN_KEY] is False


def test_skill_trigger_clears_stale_when_skill_removed_from_config(patch_context_lookup):
    patch_context_lookup(_context_no_plan())
    state = {"messages": [_ai_read_skill_md("was-cfg")], SKILL_MD_READ_WITHOUT_PLAN_KEY: True}
    out = plan_enforcer(state, _runtime(), require_plan_skills=["other"])  # type: ignore[arg-type]
    assert out[SKILL_MD_READ_WITHOUT_PLAN_KEY] is False


# ---------------------------------------------------------------------------
# tool-call trigger
# ---------------------------------------------------------------------------


def test_tool_trigger_writes_count_and_threshold(patch_context_lookup):
    patch_context_lookup(_context_no_plan())
    state = {"messages": [_tool_msg("1"), _tool_msg("2"), _tool_msg("3")]}
    out = plan_enforcer(state, _runtime(), tool_call_threshold=4)  # type: ignore[arg-type]
    assert out[TOOL_CALL_COUNT_KEY] == 3
    assert out[PLAN_REQUIRED_THRESHOLD_KEY] == 4


def test_tool_trigger_count_below_threshold_does_not_set_skill_flag(patch_context_lookup):
    """count < threshold → tool_count/threshold 写入，但 skill 标志 False（由 prompt builder 判断升级）。"""
    patch_context_lookup(_context_no_plan())
    state = {"messages": [_tool_msg("1"), _tool_msg("2")]}
    out = plan_enforcer(state, _runtime(), tool_call_threshold=4)  # type: ignore[arg-type]
    assert out[TOOL_CALL_COUNT_KEY] == 2
    assert out[PLAN_REQUIRED_THRESHOLD_KEY] == 4
    assert out[SKILL_MD_READ_WITHOUT_PLAN_KEY] is False


def test_tool_trigger_count_at_threshold(patch_context_lookup):
    patch_context_lookup(_context_no_plan())
    state = {"messages": [_tool_msg(i) for i in range(5)]}
    out = plan_enforcer(state, _runtime(), tool_call_threshold=4)  # type: ignore[arg-type]
    assert out[TOOL_CALL_COUNT_KEY] == 5
    assert out[PLAN_REQUIRED_THRESHOLD_KEY] == 4


def test_tool_trigger_false_when_plan_exists(patch_context_lookup):
    patch_context_lookup(_context_with_plan())
    state = {"messages": [_tool_msg(i) for i in range(5)]}
    out = plan_enforcer(state, _runtime(), tool_call_threshold=4)  # type: ignore[arg-type]
    assert out[TOOL_CALL_COUNT_KEY] == 0
    assert out[PLAN_REQUIRED_THRESHOLD_KEY] == 0


def test_tool_trigger_threshold_zero_disables_trigger(patch_context_lookup):
    """tool_call_threshold=0 视为关闭 tool-call 触发（None 语义），count/threshold 清零。"""
    patch_context_lookup(_context_no_plan())
    state = {"messages": [_tool_msg("1"), _tool_msg("2")]}
    out = plan_enforcer(state, _runtime(), tool_call_threshold=0)  # type: ignore[arg-type]
    # 0 视为 falsy → threshold_enabled=False → 清零
    assert out[TOOL_CALL_COUNT_KEY] == 0
    assert out[PLAN_REQUIRED_THRESHOLD_KEY] == 0


# ---------------------------------------------------------------------------
# both triggers / neither
# ---------------------------------------------------------------------------


def test_both_triggers_independent(patch_context_lookup):
    """skill + tool-call 同时配置：两条路径各自写各自标志。"""
    patch_context_lookup(_context_no_plan())
    state = {
        "messages": [
            _ai_read_skill_md("create-exp"),
            _tool_msg("1"),
            _tool_msg("2"),
            _tool_msg("3"),
            _tool_msg("4"),
            _tool_msg("5"),
        ]
    }
    out = plan_enforcer(state, _runtime(), require_plan_skills=["create-exp"], tool_call_threshold=4)  # type: ignore[arg-type]
    assert out[SKILL_MD_READ_WITHOUT_PLAN_KEY] is True
    assert out[TOOL_CALL_COUNT_KEY] == 5
    assert out[PLAN_REQUIRED_THRESHOLD_KEY] == 4


def test_neither_configured_is_noop(patch_context_lookup):
    patch_context_lookup(_context_no_plan())
    state = {"messages": [_ai_read_skill_md("x"), _tool_msg("1")], SKILL_MD_READ_WITHOUT_PLAN_KEY: True}
    out = plan_enforcer(state, _runtime())  # type: ignore[arg-type]
    assert out[SKILL_MD_READ_WITHOUT_PLAN_KEY] is False
    assert out[TOOL_CALL_COUNT_KEY] == 0
    assert out[PLAN_REQUIRED_THRESHOLD_KEY] == 0


def test_context_none_skips_enforcement(patch_context_lookup):
    patch_context_lookup(None)
    state = {"messages": [_ai_read_skill_md("create-exp"), _tool_msg("1")]}
    out = plan_enforcer(state, _runtime(), require_plan_skills=["create-exp"], tool_call_threshold=4)  # type: ignore[arg-type]
    assert out[SKILL_MD_READ_WITHOUT_PLAN_KEY] is False
    assert out[TOOL_CALL_COUNT_KEY] == 0


def test_handles_missing_messages_key(patch_context_lookup):
    patch_context_lookup(_context_no_plan())
    out = plan_enforcer({}, _runtime(), require_plan_skills=["x"], tool_call_threshold=4)  # type: ignore[arg-type]
    assert out[SKILL_MD_READ_WITHOUT_PLAN_KEY] is False
    assert out[TOOL_CALL_COUNT_KEY] == 0


# ---------------------------------------------------------------------------
# multi-turn boundary（跨 query 不误触发）
# ---------------------------------------------------------------------------


def _q1_history_with_skill_read_and_tools() -> list[Any]:
    """构造 q1 历史消息：user → 读 SKILL.md → 5 次 tool 调用 → 最终答复。"""
    return [
        HumanMessage(content="q1 创建实验"),
        _ai_read_skill_md("create-exp", tc_id="q1-tc0"),
        _tool_msg("q1-tc0"),
        _ai_other_tool("bash"),
        _tool_msg("q1-tc1"),
        _tool_msg("q1-tc2"),
        _tool_msg("q1-tc3"),
        _tool_msg("q1-tc4"),
        AIMessage(content="q1 done"),
    ]


def test_iter1_pending_does_not_count_prior_query_tools(patch_context_lookup):
    """q2 首轮迭代（pending=True）：user 消息尚未 sync，q1 的 5 个 tool 不计入 → count=0。"""
    patch_context_lookup(_context_no_plan())
    state = {"messages": _q1_history_with_skill_read_and_tools()}
    out = plan_enforcer(state, _runtime(pending=True), require_plan_skills=["create-exp"], tool_call_threshold=4)  # type: ignore[arg-type]
    assert out[TOOL_CALL_COUNT_KEY] == 0
    assert out[PLAN_REQUIRED_THRESHOLD_KEY] == 4


def test_iter1_pending_does_not_trigger_skill_from_prior_query(patch_context_lookup):
    """q2 首轮迭代：q1 读过 create-exp SKILL.md 不应触发 skill 强制（当前轮次无读取）。"""
    patch_context_lookup(_context_no_plan())
    state = {"messages": _q1_history_with_skill_read_and_tools()}
    out = plan_enforcer(state, _runtime(pending=True), require_plan_skills=["create-exp"], tool_call_threshold=4)  # type: ignore[arg-type]
    assert out[SKILL_MD_READ_WITHOUT_PLAN_KEY] is False


def test_iter2_counts_only_current_query_tools(patch_context_lookup):
    """q2 第 2 轮迭代（pending=False，user 已 sync）：只计 q2 user 之后的 tool 调用。"""
    patch_context_lookup(_context_no_plan())
    state = {
        "messages": [
            *_q1_history_with_skill_read_and_tools(),  # q1 历史（含 5 tools）
            HumanMessage(content="q2 查一下细胞数"),  # 当前 query user 消息
            _ai_other_tool("bash"),
            _tool_msg("q2-tc1"),
        ]
    }
    out = plan_enforcer(state, _runtime(pending=False), require_plan_skills=["create-exp"], tool_call_threshold=4)  # type: ignore[arg-type]
    # 只计 q2 user 之后的 1 个 tool，q1 的 5 个不计入
    assert out[TOOL_CALL_COUNT_KEY] == 1


def test_iter2_skill_trigger_only_from_current_query(patch_context_lookup):
    """q2 第 2 轮：q1 读过 SKILL.md 不触发；q2 当前轮读 create-exp 才触发。"""
    patch_context_lookup(_context_no_plan())
    state = {
        "messages": [
            *_q1_history_with_skill_read_and_tools(),  # q1 含 create-exp 读取
            HumanMessage(content="q2 再建一次实验"),
            _ai_read_skill_md("create-exp", tc_id="q2-tc0"),  # q2 当前轮读取
            _tool_msg("q2-tc0"),
        ]
    }
    out = plan_enforcer(state, _runtime(pending=False), require_plan_skills=["create-exp"])  # type: ignore[arg-type]
    assert out[SKILL_MD_READ_WITHOUT_PLAN_KEY] is True


def test_iter2_skill_not_triggered_when_only_prior_query_read(patch_context_lookup):
    """q2 第 2 轮：只有 q1 读过 SKILL.md（当前轮未读）→ skill 标志 False。"""
    patch_context_lookup(_context_no_plan())
    state = {
        "messages": [
            *_q1_history_with_skill_read_and_tools(),  # q1 含 create-exp 读取
            HumanMessage(content="q2 查细胞数"),
            _ai_other_tool("bash"),
            _tool_msg("q2-tc1"),
        ]
    }
    out = plan_enforcer(state, _runtime(pending=False), require_plan_skills=["create-exp"])  # type: ignore[arg-type]
    assert out[SKILL_MD_READ_WITHOUT_PLAN_KEY] is False


def test_iter2_current_query_tools_reach_threshold_triggers(patch_context_lookup):
    """q2 当前轮内 tool 调用数 ≥ 阈值 → 触发（q1 的不计入）。"""
    patch_context_lookup(_context_no_plan())
    state = {
        "messages": [
            *_q1_history_with_skill_read_and_tools(),  # q1 含 5 tools（不影响）
            HumanMessage(content="q2 多步分析"),
            *[_tool_msg(f"q2-tc{i}") for i in range(4)],  # q2 当前轮 4 tools
        ]
    }
    out = plan_enforcer(state, _runtime(pending=False), tool_call_threshold=4)  # type: ignore[arg-type]
    assert out[TOOL_CALL_COUNT_KEY] == 4
    assert out[PLAN_REQUIRED_THRESHOLD_KEY] == 4


# ---------------------------------------------------------------------------
# framework: _validate_hook + _bind_hook_config
# ---------------------------------------------------------------------------


def test_validate_hook_allows_keyword_only_with_default():
    def hook(state, runtime, *, a=None, b=None):  # type: ignore[no-untyped-def]
        return state

    assert BaseAgent._validate_hook(hook, "test") is hook


def test_validate_hook_rejects_extra_positional():
    def hook(state, runtime, extra):  # type: ignore[no-untyped-def]
        return state

    with pytest.raises(TypeError, match="keyword-only"):
        BaseAgent._validate_hook(hook, "test")


def test_bind_hook_config_binds_both_fields():
    received: dict[str, Any] = {}

    def hook(state, runtime, *, require_plan_skills=None, tool_call_threshold=None):  # type: ignore[no-untyped-def]
        received["require_plan_skills"] = require_plan_skills
        received["tool_call_threshold"] = tool_call_threshold
        return state

    bound = BaseAgent._bind_hook_config(
        hook, {"name": "x", "require_plan_skills": ["a"], "tool_call_threshold": 4}, location="t"
    )
    bound(state={"s": 1}, runtime=None)  # type: ignore[arg-type]
    assert received == {"require_plan_skills": ["a"], "tool_call_threshold": 4}


def test_bind_hook_config_rejects_unknown_field():
    def hook(state, runtime, *, require_plan_skills=None):  # type: ignore[no-untyped-def]
        return state

    with pytest.raises(TypeError, match="not accepted"):
        BaseAgent._bind_hook_config(hook, {"name": "x", "bogus": 1}, location="t")


def test_bind_hook_config_passthrough_when_no_config():
    def hook(state, runtime):  # type: ignore[no-untyped-def]
        return state

    assert BaseAgent._bind_hook_config(hook, {"name": "x"}, location="t") is hook


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registry_resolves_plan_enforcer():
    assert resolve_builtin_hook("plan_enforcer") is plan_enforcer
