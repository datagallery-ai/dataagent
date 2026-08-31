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
"""Unit tests for the HITL auto resolver hook."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.core.flex.hooks.hitl_auto_resolver import (
    _extract_hil_question,
    _extract_original_query,
    _find_human_feedback_call,
    _generate_answer,
    _inject_tool_message,
    _is_auto_resolve_enabled,
    _match_golden_sql_by_path,
    hitl_auto_resolver,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


GOLDEN_SQL = "SELECT user_id, COUNT(*) as order_count FROM orders GROUP BY user_id"


@pytest.fixture
def task_index_file():
    """Create temp dir with JSON index and golden SQL files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir)
        # Golden SQL files (放在 tmpdir 下)
        (p / "q01.sql").write_text("SELECT * FROM feature_q01", encoding="utf-8")
        (p / "q02.sql").write_text("SELECT * FROM feature_q02", encoding="utf-8")

        # JSON index file with golden_sql_path 使用相对路径（相对于 JSON 文件所在目录）
        index_data = [
            {
                "task_id": "0",
                "name": "q01",
                "query": "查询用户订单数，统计每个用户的订单总量",
                "golden_sql_path": "q01.sql",  # 相对路径
            },
            {
                "task_id": "1",
                "name": "q02",
                "query": "计算月销售额，汇总所有产品的月度收入",
                "golden_sql_path": "q02.sql",  # 相对路径
            },
        ]
        index_file = p / "task_index.json"
        index_file.write_text(__import__("json").dumps(index_data), encoding="utf-8")

        yield str(index_file)


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
        task_index_path: str | None = None,
    ):
        self._config: dict[str, Any] = {
            "AGENT_CONFIG": {
                "hitl_auto_resolve": auto_resolve,
                "hitl_task_index_path": task_index_path,
            }
        }

    def get_all_config(self) -> dict[str, Any]:
        return self._config

    def llm(self, name: str = "planner") -> MockLLM:
        return MockLLM()


# ─────────────────────────────────────────────────────────────────────────────
# State helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_state(
    messages: list,
    hitl_auto_resolve: bool = True,
    hitl_task_index_path: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "messages": messages,
        "hitl_auto_resolve": hitl_auto_resolve,
        "hitl_task_index_path": hitl_task_index_path,
        "complete": False,
        "need_human_feedback": True,
    }
    state.update(extra)
    return state


def _make_hil_tool_call(
    call_id: str = "call_123", reason: str = "请确认", pending_action: str = "数据口径是否正确"
) -> dict:
    return {
        "id": call_id,
        "name": "request_human_feedback",
        "args": {"reason": reason, "pending_action": pending_action},
        "type": "tool_call",
    }


def _make_ai_message_with_hil(content: str = "完成", call_id: str = "call_123") -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=[_make_hil_tool_call(call_id=call_id)],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests for helper functions
# ─────────────────────────────────────────────────────────────────────────────


class TestFindHumanFeedbackCall:
    def test_finds_tool_call_in_aimessage(self):
        messages = [_make_ai_message_with_hil()]
        result = _find_human_feedback_call(messages)
        assert result is not None
        assert result["name"] == "request_human_feedback"

    def test_returns_none_when_no_hil_call(self):
        messages = [AIMessage(content="hello")]
        assert _find_human_feedback_call(messages) is None

    def test_returns_last_hil_call(self):
        messages = [
            AIMessage(content="", tool_calls=[_make_hil_tool_call(call_id="c1")]),
            AIMessage(content="", tool_calls=[_make_hil_tool_call(call_id="c2")]),
        ]
        result = _find_human_feedback_call(messages)
        assert result is not None
        assert result["id"] == "c2"


class TestExtractOriginalQuery:
    def test_extracts_from_human_message(self):
        messages = [HumanMessage(content="<user_query>查询用户订单数</user_query>")]
        assert _extract_original_query(messages) == "查询用户订单数"

    def test_returns_empty_for_empty_messages(self):
        assert _extract_original_query([]) == ""

    def test_returns_empty_when_first_not_human(self):
        messages = [AIMessage(content="hello")]
        assert _extract_original_query(messages) == ""


class TestExtractHilQuestion:
    def test_extracts_pending_action(self):
        call = _make_hil_tool_call(pending_action="请确认数据口径", reason="需要确认")
        assert _extract_hil_question(call) == "请确认数据口径"

    def test_falls_back_to_reason(self):
        call = _make_hil_tool_call(pending_action="", reason="需要确认")
        assert _extract_hil_question(call) == "需要确认"


class TestMatchGoldenSqlByPath:
    def test_matches_by_path(self, task_index_file):
        query = "查询用户订单数，统计每个用户的订单总量"
        result, matched_task = _match_golden_sql_by_path(query, task_index_file)
        assert result == "SELECT * FROM feature_q01"
        assert matched_task["name"] == "q01"

    def test_matches_similar_query(self, task_index_file):
        """Query shares keywords with task.query (after extraction)"""
        # 模拟从 <user_query>"query": "...",</user_query> 提取后的纯 query
        query = "用户订单数统计"
        result, matched_task = _match_golden_sql_by_path(query, task_index_file)
        assert result == "SELECT * FROM feature_q01"
        assert matched_task["name"] == "q01"

    def test_returns_none_when_no_match(self, task_index_file):
        result, matched_task = _match_golden_sql_by_path("完全不相关的查询内容 xyz123", task_index_file)
        assert result is None
        assert matched_task is None

    def test_returns_none_for_invalid_index_path(self):
        result, matched_task = _match_golden_sql_by_path("查询", "/nonexistent/index.json")
        assert result is None
        assert matched_task is None

    def test_relative_path_resolved_from_json_dir(self):
        """Test that relative golden_sql_path is resolved from JSON file's directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            (p / "golden").mkdir()
            (p / "golden" / "q01.sql").write_text("SELECT * FROM relative_path_test", encoding="utf-8")

            index_data = [
                {
                    "task_id": "0",
                    "name": "q01",
                    "query": "相对路径测试",
                    "golden_sql_path": "golden/q01.sql",  # 相对路径子目录
                },
            ]
            index_file = p / "task_index.json"
            index_file.write_text(__import__("json").dumps(index_data), encoding="utf-8")

            result, matched_task = _match_golden_sql_by_path("相对路径测试", str(index_file))
            assert result == "SELECT * FROM relative_path_test"
            assert matched_task["name"] == "q01"


class TestInjectToolMessage:
    def test_injects_tool_message_at_end(self):
        messages = [HumanMessage(content="hello"), AIMessage(content="world")]
        updated = _inject_tool_message(messages, "call_123", "auto reply")
        assert len(updated) == 3
        assert isinstance(updated[-1], ToolMessage)
        assert updated[-1].content == "auto reply"
        assert updated[-1].tool_call_id == "call_123"


# ─────────────────────────────────────────────────────────────────────────────
# Tests for main hook function
# ─────────────────────────────────────────────────────────────────────────────


class TestHitlAutoResolver:
    def test_skips_when_disabled(self, task_index_file):
        state = _make_state(
            messages=[HumanMessage(content="查询用户订单数"), _make_ai_message_with_hil()],
            hitl_auto_resolve=False,
            hitl_task_index_path=task_index_file,
        )
        runtime = RuntimeStub(auto_resolve=False, task_index_path=task_index_file)
        result = hitl_auto_resolver(cast(Any, state), cast(Any, runtime))
        assert result is state

    def test_skips_when_no_hil_call(self, task_index_file):
        state = _make_state(
            messages=[HumanMessage(content="查询用户订单数"), AIMessage(content="完成")],
            hitl_task_index_path=task_index_file,
        )
        result = hitl_auto_resolver(cast(Any, state), cast(Any, RuntimeStub(task_index_path=task_index_file)))
        assert result is state

    def test_skips_when_no_golden_sql_match(self, task_index_file):
        state = _make_state(
            messages=[HumanMessage(content="完全不相关的查询"), _make_ai_message_with_hil()],
            hitl_task_index_path=task_index_file,
        )
        result = hitl_auto_resolver(cast(Any, state), cast(Any, RuntimeStub(task_index_path=task_index_file)))
        assert result is state

    def test_resolves_hil_and_marks_hitl_resolved(self, task_index_file):
        state = _make_state(
            messages=[
                HumanMessage(content="<user_query>查询用户订单数</user_query>"),
                _make_ai_message_with_hil(),
            ],
            hitl_task_index_path=task_index_file,
        )
        runtime = RuntimeStub(task_index_path=task_index_file)
        result = hitl_auto_resolver(cast(Any, state), cast(Any, runtime))

        # Should inject tool message and mark as resolved (not complete, let workflow continue)
        assert result["hitl_auto_resolved"] is True
        assert result["need_human_feedback"] is False
        assert len(result["messages"]) == 3
        assert isinstance(result["messages"][-1], ToolMessage)
        assert result["messages"][-1].content == "这是自动回复内容"

    def test_uses_runtime_fallback_for_config(self, task_index_file):
        state = _make_state(
            messages=[
                HumanMessage(content="<user_query>查询用户订单数</user_query>"),
                _make_ai_message_with_hil(),
            ],
            hitl_auto_resolve=False,  # not in state
            hitl_task_index_path=None,
        )
        runtime = RuntimeStub(auto_resolve=True, task_index_path=task_index_file)
        result = hitl_auto_resolver(cast(Any, state), cast(Any, runtime))
        assert result["hitl_auto_resolved"] is True
        assert result["need_human_feedback"] is False

    def test_skips_with_no_runtime_and_no_state_config(self):
        state = _make_state(
            messages=[HumanMessage(content="查询"), _make_ai_message_with_hil()],
            hitl_auto_resolve=False,
            hitl_task_index_path=None,
        )
        result = hitl_auto_resolver(cast(Any, state), None)
        assert result is state


class TestIsAutoResolveEnabled:
    def test_true_from_state(self):
        state = {"hitl_auto_resolve": True}
        assert _is_auto_resolve_enabled(state, None, None) is True

    def test_false_from_state(self):
        state = {"hitl_auto_resolve": False}
        assert _is_auto_resolve_enabled(state, None, None) is False

    def test_true_from_runtime(self):
        runtime = RuntimeStub(auto_resolve=True)
        assert _is_auto_resolve_enabled({}, None, runtime) is True

    def test_false_from_runtime(self):
        runtime = RuntimeStub(auto_resolve=False)
        assert _is_auto_resolve_enabled({}, None, runtime) is False
