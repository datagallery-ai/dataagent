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
"""Tests for the layered IR-candidate and Fold compression flow."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

from dataagent.core.flex.hooks.pruner import pruner


class _CachedContext:
    def __init__(self, summaries: dict[str, str]) -> None:
        self.ir_summary_cache = summaries


class _RecordingLlm:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke(self, messages: list[HumanMessage], **_: Any) -> AIMessage:
        """Record the Fold prompt and return a deterministic short summary."""
        self.prompts.append(str(messages[0].content))
        return AIMessage(content="folded summary")


class _Runtime:
    def __init__(self, *, token_limit: int, message_cnt: int, recent_turns: int = 1) -> None:
        self.env = SimpleNamespace(
            compress_token_limit=token_limit,
            compress_message_cnt=message_cnt,
            ir_recent_turns=recent_turns,
        )
        self.fold_llm = _RecordingLlm()
        self.llm_calls = 0

    def llm(self, _: str) -> _RecordingLlm:
        """Return the recording Fold LLM and count lazy acquisitions."""
        self.llm_calls += 1
        return self.fold_llm


def _state(messages: list[Any]) -> dict[str, Any]:
    return {
        "messages": messages,
        "user_id": "user",
        "session_id": "session",
        "run_id": 0,
        "sub_id": 0,
    }


def test_below_trigger_keeps_history_append_only(monkeypatch) -> None:
    """A normal planner round must not replace history or inspect Context."""
    messages = [HumanMessage(content="small history")]
    state = _state(messages)
    runtime = _Runtime(token_limit=1_024, message_cnt=5)

    def _unexpected_context_lookup(*_: Any, **__: Any) -> None:
        raise AssertionError("normal rounds must not build an IR candidate")

    monkeypatch.setattr("dataagent.core.flex.hooks.pruner.get_context_for_flex_state", _unexpected_context_lookup)

    result = pruner(state, runtime)

    assert result.get("messages") == messages
    assert runtime.llm_calls == 0


def test_message_overflow_skips_ir_candidate_and_folds_to_sixty_percent(monkeypatch) -> None:
    """Message pressure must go directly to Fold and reduce history to 0.6M."""
    messages = [HumanMessage(content=f"message {index}") for index in range(6)]
    state = _state(messages)
    runtime = _Runtime(token_limit=100_000, message_cnt=5)

    def _unexpected_context_lookup(*_: Any, **__: Any) -> None:
        raise AssertionError("message-only compression must not build an IR candidate")

    monkeypatch.setattr("dataagent.core.flex.hooks.pruner.get_context_for_flex_state", _unexpected_context_lookup)

    result = pruner(state, runtime)

    assert isinstance(result.get("messages", [])[0], RemoveMessage)
    assert len(result.get("messages", [])[1:]) <= 3
    assert runtime.llm_calls == 1


def test_combined_pressure_fold_meets_both_low_water_targets(monkeypatch) -> None:
    """A direct Fold triggered by both causes must satisfy both 0.6 targets."""
    messages = [HumanMessage(content="protected head")]
    messages.extend(HumanMessage(content=f"message {index} " + ("value " * 1_000)) for index in range(5))
    state = _state(messages)
    runtime = _Runtime(token_limit=1_024, message_cnt=5)

    def _unexpected_context_lookup(*_: Any, **__: Any) -> None:
        raise AssertionError("message pressure must bypass IR even when token pressure is also active")

    monkeypatch.setattr("dataagent.core.flex.hooks.pruner.get_context_for_flex_state", _unexpected_context_lookup)

    result = pruner(state, runtime)
    committed = result.get("messages", [])[1:]

    assert len(committed) <= 3
    assert count_tokens_approximately(committed) <= int(1_024 * 0.6)


def test_token_overflow_commits_acceptable_ir_candidate_without_fold(monkeypatch) -> None:
    """An IR candidate below 0.6T must be the event's only state replacement."""
    raw_content = "old raw payload " + ("value " * 3_000)
    messages = [
        AIMessage(content="old turn"),
        ToolMessage(content=raw_content, tool_call_id="old-tool", name="read_file"),
        AIMessage(content="recent turn"),
    ]
    state = _state(messages)
    runtime = _Runtime(token_limit=1_024, message_cnt=20)
    context = _CachedContext({"old-tool": "[IR Summary] compact old result"})
    monkeypatch.setattr(
        "dataagent.core.flex.hooks.pruner.get_context_for_flex_state",
        lambda *_args, **_kwargs: context,
    )

    result = pruner(state, runtime)
    committed = result.get("messages", [])[1:]

    assert isinstance(result.get("messages", [])[0], RemoveMessage)
    assert len(committed) == len(messages)
    assert committed[1].content == "[IR Summary] compact old result"
    assert messages[1].content == raw_content
    assert runtime.llm_calls == 0


def test_rejected_ir_candidate_folds_original_history(monkeypatch) -> None:
    """A candidate above 0.6T must be discarded before Fold reads the raw history."""
    raw_marker = "ORIGINAL_RAW_TOOL_PAYLOAD"
    raw_content = raw_marker + (" value" * 3_000)
    oversized_ir = "[IR Summary]" + (" summary" * 3_000)
    messages = [
        AIMessage(content="old turn"),
        ToolMessage(content=raw_content, tool_call_id="old-tool", name="read_file"),
        AIMessage(content="recent turn"),
    ]
    state = _state(messages)
    runtime = _Runtime(token_limit=1_024, message_cnt=20)
    context = _CachedContext({"old-tool": oversized_ir})
    monkeypatch.setattr(
        "dataagent.core.flex.hooks.pruner.get_context_for_flex_state",
        lambda *_args, **_kwargs: context,
    )

    result = pruner(state, runtime)

    assert isinstance(result.get("messages", [])[0], RemoveMessage)
    assert runtime.llm_calls == 1
    assert raw_marker in runtime.fold_llm.prompts[0]
    assert oversized_ir not in runtime.fold_llm.prompts[0]
