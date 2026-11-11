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
"""de_agent suite ir_question_guard hook unit tests."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.core.suite.builtin_suites.de_agent.hooks.ir_question_guard import (
    ir_question_guard,
)


class _RuntimeStub:
    def __init__(self, *, hitl_enabled: bool = True, queue: list[dict[str, Any]] | None = None):
        self._config: dict[str, Any] = {
            "AGENT_CONFIG": {"enable_human_feedback": hitl_enabled},
        }
        self._cache: dict[str, Any] = {
            "ir_hitl_queue": queue if queue is not None else [{
                "field_id": "identity",
                "title": "特征主键",
                "question": "主键用 device_id 还是 user_id？",
                "unresolved": ["主键未定"],
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


class TestIrQuestionGuard:
    def test_converts_complete_turn_when_queue_pending(self):
        state = _state([HumanMessage(content="需求"), _ai("准备交付")])
        runtime = _RuntimeStub()
        result = ir_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result["complete"] is False
        assert result["need_human_feedback"] is True
        last_ai = [m for m in result["messages"] if isinstance(m, AIMessage)][-1]
        assert last_ai.tool_calls[-1]["name"] == "request_human_feedback"
        assert "device_id" in last_ai.tool_calls[-1]["args"]["pending_action"]
        assert runtime.get_cache("ir_question_guard.fire_count") == 1

    def test_rewrites_nl2sql_consumer_call(self):
        state = _state(
            [_ai(tool_calls=[{
                "id": "c1",
                "name": "wrapped_nl2sql_sub_agent_tool",
                "args": {"query": "x"},
            }])],
            complete=False,
        )
        runtime = _RuntimeStub()
        result = ir_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result["need_human_feedback"] is True
        last_ai = result["messages"][-1]
        names = [c["name"] for c in last_ai.tool_calls]
        assert "wrapped_nl2sql_sub_agent_tool" not in names
        assert names == ["request_human_feedback"]

    def test_drops_sibling_non_consumer_tool_calls(self):
        """Parallel IR-consumer + other tools must not leave dangling tool_call_ids."""
        state = _state(
            [_ai(tool_calls=[
                {"id": "c1", "name": "wrapped_nl2sql_sub_agent_tool", "args": {"query": "x"}},
                {"id": "c2", "name": "metadata_recall", "args": {}},
            ])],
            complete=False,
        )
        runtime = _RuntimeStub()
        result = ir_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result["need_human_feedback"] is True
        last_ai = result["messages"][-1]
        assert [c["name"] for c in last_ai.tool_calls] == ["request_human_feedback"]
        assert last_ai.invalid_tool_calls == []

    def test_skips_when_already_asking_hitl(self):
        state = _state([_ai(tool_calls=[{
            "id": "c1",
            "name": "request_human_feedback",
            "args": {"reason": "x", "pending_action": "y"},
        }])])
        runtime = _RuntimeStub()
        result = ir_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_skips_when_queue_empty(self):
        state = _state([_ai("done")])
        runtime = _RuntimeStub(queue=[])
        result = ir_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_skips_when_hitl_disabled(self):
        state = _state([_ai("done")], enable_human_feedback=False)
        runtime = _RuntimeStub(hitl_enabled=False)
        result = ir_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_skips_when_budget_exhausted(self):
        state = _state([_ai("done")], hitl_count=4, hitl_budget=4)
        runtime = _RuntimeStub()
        result = ir_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_skips_mid_work_non_consumer_tools(self):
        state = _state(
            [_ai(tool_calls=[{"id": "c1", "name": "metadata_recall", "args": {}}])],
            complete=False,
        )
        runtime = _RuntimeStub()
        result = ir_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_skips_when_feedback_arrived_since_queue_sync(self):
        """Stale pre-answer queue must not rewrite a post-HITL consumer call."""
        state = _state(
            [
                ToolMessage(
                    content="确认使用 device_id 作为主键",
                    tool_call_id="c1",
                    name="request_human_feedback",
                ),
                _ai(tool_calls=[{
                    "id": "c2",
                    "name": "wrapped_nl2sql_sub_agent_tool",
                    "args": {"query": "x"},
                }]),
            ],
            complete=False,
        )
        runtime = _RuntimeStub()
        # Queue present without a matching feedback digest ⇒ treat as pre-answer stale.
        result = ir_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_skips_when_same_fingerprint_asks_exhausted(self):
        state = _state(
            [_ai(tool_calls=[{
                "id": "c1",
                "name": "wrapped_nl2sql_sub_agent_tool",
                "args": {"query": "x"},
            }])],
            complete=False,
        )
        runtime = _RuntimeStub()
        runtime.set_cache("ir_hitl_fingerprint", "fp-same")
        runtime.set_cache("ir_hitl_fingerprint_ask_counts", {"fp-same": 2})
        # Explicit empty feedback digest so queue is not treated as stale.
        import hashlib
        import json

        empty = hashlib.sha256(
            json.dumps([], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        runtime.set_cache("ir_hitl_feedback_digest", empty)
        result = ir_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state
        assert runtime.get_cache("ir_hitl_best_effort") is True

    def test_bumps_fingerprint_ask_count_on_fire(self):
        import hashlib
        import json

        state = _state([_ai("准备交付")])
        runtime = _RuntimeStub()
        empty = hashlib.sha256(
            json.dumps([], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        runtime.set_cache("ir_hitl_feedback_digest", empty)
        runtime.set_cache("ir_hitl_fingerprint", "fp-1")
        result = ir_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result["need_human_feedback"] is True
        assert runtime.get_cache("ir_hitl_fingerprint_ask_counts")["fp-1"] == 1
        assert runtime.get_cache("ir_hitl_field_ask_counts")["identity"] == 1

    def test_skips_when_same_field_asks_exhausted(self):
        import hashlib
        import json

        state = _state(
            [_ai(tool_calls=[{
                "id": "c1",
                "name": "wrapped_nl2sql_sub_agent_tool",
                "args": {"query": "x"},
            }])],
            complete=False,
        )
        runtime = _RuntimeStub()
        empty = hashlib.sha256(
            json.dumps([], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        runtime.set_cache("ir_hitl_feedback_digest", empty)
        runtime.set_cache("ir_hitl_fingerprint", "fp-drifted")
        runtime.set_cache("ir_hitl_field_ask_counts", {"identity": 2})
        result = ir_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state
        assert runtime.get_cache("ir_hitl_best_effort") is True
        assert runtime.get_cache("ir_hitl_queue") == []

    def test_caps_at_two_fires(self):
        state = _state([_ai("done")])
        runtime = _RuntimeStub()
        runtime.set_cache("ir_question_guard.fire_count", 2)
        result = ir_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_caps_respect_yaml_max_fires(self):
        state = _state([_ai("done")])
        runtime = _RuntimeStub()
        runtime._config["DATA_TASK_IR"] = {"hitl_question_guard_max_fires": 1}
        runtime.set_cache("ir_question_guard.fire_count", 1)
        result = ir_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_skips_when_enable_ir_hitl_false(self):
        state = _state([_ai("准备交付")])
        runtime = _RuntimeStub()
        runtime._config["DATA_TASK_IR"] = {"enable_ir_hitl": False}
        result = ir_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result is state

    def test_interactive_still_fires(self):
        """Unlike text_question_guard, interactive HITL (no auto_resolve) still triggers."""
        state = _state([_ai("准备交付")])
        runtime = _RuntimeStub()
        result = ir_question_guard(state, runtime)  # type: ignore[arg-type]
        assert result["need_human_feedback"] is True
