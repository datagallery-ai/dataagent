"""Validate inline reasoning normalization without calling or simulating an LLM."""

from types import SimpleNamespace

import pytest

from dataagent.core.managers.llm_manager.adapters import (
    LangChainChatModelAdapter,
    LLMResponse,
    _StreamAccum,
    split_inline_reasoning,
)


@pytest.mark.parametrize(
    ("content", "reasoning", "expected_content", "expected_reasoning"),
    [
        ("analysis</think>SELECT 1", "", "SELECT 1", "analysis"),
        ("<think>analysis</think>\nSELECT 1", "", "SELECT 1", "analysis"),
        ("analysis</think>answer", "native", "answer", "native\nanalysis"),
        ("</think>answer", "native", "answer", "native"),
        ("  ordinary answer\n", "native", "  ordinary answer\n", "native"),
        ("<think>unfinished", "", "<think>unfinished", ""),
        ("analysis</think>", "", "", "analysis"),
    ],
)
def test_split_inline_reasoning(content, reasoning, expected_content, expected_reasoning):
    """Preserve unmarked text and split only responses with a closing marker."""
    assert split_inline_reasoning(content, reasoning) == (expected_content, expected_reasoning)


def test_complete_response_preserves_metadata():
    """Normalize complete outputs while retaining usage, tools, and the original response."""
    raw = SimpleNamespace(
        content="<think>analysis</think>answer",
        reasoning_content="native",
        usage_metadata={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        tool_calls=[{"id": "call-1", "name": "query", "args": {}}],
    )
    response = LangChainChatModelAdapter._wrap_output(raw)
    assert response.content == "answer"
    assert response.reasoning_content == "native\nanalysis"
    assert response.usage_metadata.get("total_tokens") == 5
    assert response.tool_calls == raw.tool_calls
    assert response.raw is raw


@pytest.mark.parametrize("split_at", range(1, len("<think>analysis</think>answer")))
def test_stream_normalizes_after_merging_chunks(split_at):
    """Recognize the marker at every chunk boundary without losing text or duplicating reasoning."""
    text = "<think>analysis</think>answer"
    accumulated = _StreamAccum()
    for content in (text[:split_at], text[split_at:]):
        chunk = LangChainChatModelAdapter._wrap_output(SimpleNamespace(content=content), complete=False)
        assert chunk.content == content
        accumulated.append_chunk(chunk)
    accumulated.append_chunk(LLMResponse(content="", reasoning_content="native", usage_metadata={"total_tokens": 5}))
    response = accumulated.to_llm_response()
    assert response.content == "answer"
    assert response.reasoning_content == "native\nanalysis"
    assert response.usage_metadata.get("total_tokens") == 5
