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
"""de_agent suite text_question_guard hook 单元测试（P1: 文本提问死胡同守卫）。"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from dataagent.core.suite.builtin_suites.de_agent.hooks.text_question_guard import (
    _ends_awaiting_user,
    text_question_guard,
)


class TestEndsAwaitingUser:
    def test_dead_end_question_fires(self):
        assert _ends_awaiting_user("我的建议：采用方案 A。请问您确认使用方案 A 吗？")

    def test_which_option_question_fires(self):
        assert _ends_awaiting_user("方案 B 缺少字段。请问您选择哪个方案？")

    def test_english_question_mark_fires(self):
        assert _ends_awaiting_user("我倾向方案A，您能确认吗?")

    def test_delivery_statement_passes(self):
        assert not _ends_awaiting_user(
            "4. 序列分隔符：键值对序列使用 ; 分隔，键和值之间使用 : 分隔。"
        )

    def test_delivery_with_summary_passes(self):
        assert not _ends_awaiting_user(
            "时间窗口：用户说截止今天，我采用全部历史数据（pt_d <= '$date'），未加起始日期限制。"
        )

    def test_empty_and_whitespace_pass(self):
        assert not _ends_awaiting_user("")
        assert not _ends_awaiting_user("Let me analyze the user")


class _RuntimeStub:
    """hitl_auto_resolve / enable_human_feedback / cache 三件套。"""

    def __init__(self, *, auto_resolve: bool = True, hitl_enabled: bool = True):
        self._config: dict[str, Any] = {
            "AGENT_CONFIG": {
                "hitl_auto_resolve": auto_resolve,
                "enable_human_feedback": hitl_enabled,
            }
        }
        self._cache: dict[str, Any] = {}

    def get_all_config(self) -> dict[str, Any]:
        return self._config

    def get_cache(self, key: str, default: Any = None) -> Any:
        return self._cache.get(key, default)

    def set_cache(self, key: str, value: Any) -> None:
        self._cache[key] = value


def _state(messages: list[Any], **extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = {"messages": messages, "complete": True, "need_human_feedback": False}
    state.update(extra)
    return state


def _ai(content: str, tool_calls: list[Any] | None = None) -> AIMessage:
    return AIMessage(content=content, tool_calls=tool_calls or [])


class TestTextQuestionGuard:
    DEAD_END = "方案 A 最合适。请问您确认使用方案 A 吗？"

    def test_converts_dead_end_to_hitl(self):
        state = _state([HumanMessage(content="需求"), _ai(self.DEAD_END)])
        runtime = _RuntimeStub()
        result = text_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result["complete"] is False
        assert result["need_human_feedback"] is True
        last_ai = [m for m in result["messages"] if isinstance(m, AIMessage)][-1]
        assert last_ai.tool_calls and last_ai.tool_calls[-1]["name"] == "request_human_feedback"
        assert "请问您确认" in last_ai.tool_calls[-1]["args"]["pending_action"]

    def test_delivery_passes_through(self):
        state = _state([HumanMessage(content="需求"), _ai("已完成，SQL 写入 x.sql，共 8 列。")])
        runtime = _RuntimeStub()
        result = text_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result["complete"] is True
        assert result is state

    def test_not_complete_passes_through(self):
        state = _state([_ai(self.DEAD_END)], complete=False)
        runtime = _RuntimeStub()
        result = text_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_message_with_tool_calls_passes_through(self):
        state = _state([_ai(self.DEAD_END, tool_calls=[{"id": "c1", "name": "bash", "args": {}}])])
        runtime = _RuntimeStub()
        result = text_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_interactive_session_passes_through(self):
        """交互式会话（未开 auto_resolve）不转换，用户会在下一轮直接答复。"""
        state = _state([HumanMessage(content="需求"), _ai(self.DEAD_END)])
        runtime = _RuntimeStub(auto_resolve=False)
        result = text_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_hitl_disabled_passes_through(self):
        state = _state([HumanMessage(content="需求"), _ai(self.DEAD_END)])
        runtime = _RuntimeStub(hitl_enabled=False)
        result = text_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_guard_caps_at_two_fires(self):
        state = _state([HumanMessage(content="需求"), _ai(self.DEAD_END)])
        runtime = _RuntimeStub()
        runtime.set_cache("text_question_guard.fire_count", 2)
        result = text_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_fire_counter_increments(self):
        state = _state([HumanMessage(content="需求"), _ai(self.DEAD_END)])
        runtime = _RuntimeStub()
        text_question_guard(state, runtime)  # type: ignore[arg-type]
        assert runtime.get_cache("text_question_guard.fire_count") == 1


@pytest.mark.parametrize(
    "content",
    [
        "请问您倾向哪个方案？",
        "**请问您确认选择方案 A（xx.dm）作为数据源吗？**",
        "您能进一步说明业务含义吗？",
        "以上是两处口径差异，请确认一下采用哪一种？",
    ],
)
def test_known_dead_end_patterns_from_run(content: str):
    """v3.2 run 实测死胡同结尾模式全部命中。"""
    assert _ends_awaiting_user(content)
