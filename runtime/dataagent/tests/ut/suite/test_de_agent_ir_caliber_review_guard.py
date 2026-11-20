# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""de_agent suite ir_caliber_review_guard hook unit tests."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.core.suite.builtin_suites.de_agent.hooks.ir_caliber_review_guard import (
    ir_caliber_review_guard,
)


class _RuntimeStub:
    def __init__(self, *, review_enabled: bool = True, hitl_enabled: bool = True):
        self._config: dict[str, Any] = {
            "AGENT_CONFIG": {"enable_human_feedback": hitl_enabled},
            "DATA_TASK_IR": {
                "enable_ir_hitl": True,
                "enable_ir_caliber_review": review_enabled,
            },
        }
        self._cache: dict[str, Any] = {
            "ir_hitl_queue": [],
            "ir_field_values": [{
                "field_id": "time_context",
                "value": {
                    "output_bucket": {
                        "time_field": "pt_d",
                        "grain": "day",
                        "timezone": None,
                        "calendar": None,
                        "week_start": None,
                        "custom_definition": None,
                    },
                    "input_windows": [],
                },
                "unresolved": [],
            }],
        }

    def get_all_config(self) -> dict[str, Any]:
        return self._config

    def get_config(self, key: str, default: Any = None) -> Any:
        if key.startswith("DATA_TASK_IR."):
            section = self._config.get("DATA_TASK_IR", {})
            if isinstance(section, dict):
                return section.get(key.split(".", 1)[1], default)
        return default

    def get_cache(self, key: str, default: Any = None) -> Any:
        return self._cache.get(key, default)

    def set_cache(self, key: str, value: Any) -> None:
        self._cache[key] = value


def _state(messages: Any, **extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "messages": messages,
        "complete": True,
        "need_human_feedback": False,
        "enable_human_feedback": True,
        "hitl_budget": 4,
        "hitl_count": 0,
    }
    state.update(extra)
    return state


def _ai(content: str = "继续", tool_calls: list[Any] | None = None) -> AIMessage:
    return AIMessage(content=content, tool_calls=tool_calls or [])


class TestIrCaliberReviewGuard:
    def test_default_off_is_noop(self):
        state = _state([HumanMessage(content="需求"), _ai("准备交付")])
        runtime = _RuntimeStub(review_enabled=False)
        result = ir_caliber_review_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_rewrites_nl2sql_when_enabled(self):
        state = _state(
            [_ai(tool_calls=[{
                "id": "c1",
                "name": "wrapped_nl2sql_sub_agent_tool",
                "args": {"query": "x"},
            }])],
            complete=False,
        )
        runtime = _RuntimeStub(review_enabled=True)
        result = ir_caliber_review_guard(state, runtime)  # type: ignore[arg-type]
        assert result["need_human_feedback"] is True
        last_ai = result["messages"][-1]
        assert [c["name"] for c in last_ai.tool_calls] == ["request_human_feedback"]
        pending = last_ai.tool_calls[0]["args"]["pending_action"]
        assert "口径最终检视" in pending
        assert runtime.get_cache("ir_caliber_review.asked") is True
        assert runtime.get_cache("ir_caliber_review.fire_count") == 1

    def test_skips_when_blocking_queue_pending(self):
        state = _state([_ai("准备交付")])
        runtime = _RuntimeStub(review_enabled=True)
        runtime.set_cache("ir_hitl_queue", [{
            "field_id": "identity",
            "question": "主键？",
            "unresolved": ["未定"],
        }])
        result = ir_caliber_review_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_rewrites_when_queue_pending_but_question_guard_exhausted(self):
        state = _state(
            [_ai(tool_calls=[{
                "id": "c1",
                "name": "wrapped_nl2sql_sub_agent_tool",
                "args": {"query": "x"},
            }])],
            complete=False,
        )
        runtime = _RuntimeStub(review_enabled=True)
        runtime.set_cache("ir_hitl_queue", [{
            "field_id": "time_context",
            "question": "时间范围？",
            "unresolved": ["未定"],
        }])
        runtime.set_cache("ir_question_guard.fire_count", 2)
        result = ir_caliber_review_guard(state, runtime)  # type: ignore[arg-type]
        assert result["need_human_feedback"] is True
        last_ai = result["messages"][-1]
        assert [c["name"] for c in last_ai.tool_calls] == ["request_human_feedback"]
        pending = last_ai.tool_calls[0]["args"]["pending_action"]
        assert "口径最终检视" in pending
        assert "未确认项已从约束中省略" in pending
        assert runtime.get_cache("ir_caliber_review.asked") is True
        assert runtime.get_cache("ir_caliber_review.fire_count") == 1

    def test_rewrites_when_queue_pending_but_best_effort_set(self):
        state = _state(
            [_ai(tool_calls=[{
                "id": "c1",
                "name": "wrapped_nl2sql_sub_agent_tool",
                "args": {"query": "x"},
            }])],
            complete=False,
        )
        runtime = _RuntimeStub(review_enabled=True)
        runtime.set_cache("ir_hitl_queue", [{
            "field_id": "identity",
            "question": "主键？",
            "unresolved": ["未定"],
        }])
        runtime.set_cache("ir_hitl_best_effort", True)
        result = ir_caliber_review_guard(state, runtime)  # type: ignore[arg-type]
        assert result["need_human_feedback"] is True
        pending = result["messages"][-1].tool_calls[0]["args"]["pending_action"]
        assert "口径最终检视" in pending
        assert "未确认项已从约束中省略" in pending

    def test_marks_done_after_reply_and_skips_second_ask(self):
        runtime = _RuntimeStub(review_enabled=True)
        first = ir_caliber_review_guard(_state([_ai("准备交付")]), runtime)  # type: ignore[arg-type]
        assert first["need_human_feedback"] is True

        answered = _state([
            ToolMessage(
                content="确认，按此生成",
                tool_call_id="r1",
                name="request_human_feedback",
            ),
            _ai(tool_calls=[{
                "id": "c2",
                "name": "wrapped_nl2sql_sub_agent_tool",
                "args": {"query": "x"},
            }]),
        ], complete=False)
        second = ir_caliber_review_guard(answered, runtime)  # type: ignore[arg-type]
        assert second is answered
        assert runtime.get_cache("ir_caliber_review.done") is True

    def test_fire_cap_marks_done(self):
        state = _state([_ai("准备交付")])
        runtime = _RuntimeStub(review_enabled=True)
        runtime.set_cache("ir_caliber_review.fire_count", 1)
        result = ir_caliber_review_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state
        assert runtime.get_cache("ir_caliber_review.done") is True

    def test_skips_when_human_feedback_disabled(self):
        state = _state([_ai("准备交付")], enable_human_feedback=False)
        runtime = _RuntimeStub(review_enabled=True, hitl_enabled=False)
        result = ir_caliber_review_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_reserves_one_ask_when_hitl_budget_exhausted(self):
        """选表等中途 HITL 烧光 budget 后，仍应能触发一次口径最终检视。"""
        state = _state(
            [_ai(tool_calls=[{
                "id": "c1",
                "name": "wrapped_nl2sql_sub_agent_tool",
                "args": {"query": "x"},
            }])],
            complete=False,
            hitl_budget=8,
            hitl_count=8,
        )
        runtime = _RuntimeStub(review_enabled=True)
        result = ir_caliber_review_guard(state, runtime)  # type: ignore[arg-type]
        assert result["need_human_feedback"] is True
        pending = result["messages"][-1].tool_calls[0]["args"]["pending_action"]
        assert "口径最终检视" in pending
        assert runtime.get_cache("ir_caliber_review.asked") is True
        assert runtime.get_cache("ir_caliber_review.done") is not True

    def test_budget_exhausted_after_ask_marks_done(self):
        state = _state([_ai("准备交付")], hitl_budget=4, hitl_count=4)
        runtime = _RuntimeStub(review_enabled=True)
        runtime.set_cache("ir_caliber_review.asked", True)
        runtime.set_cache("ir_caliber_review.fire_count", 1)
        result = ir_caliber_review_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state
        assert runtime.get_cache("ir_caliber_review.done") is True
