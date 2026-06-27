# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Tests for CLI streaming adapters."""

from __future__ import annotations

from typing import Any

import pytest

from dataagent.interface.cli.main import _stream_agent_response
from dataagent.utils.cli.rich_renderer import StreamRenderer


class _FakeRenderer:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.started = False
        self.stopped = False

    def start(self) -> None:
        """Record stream start."""
        self.started = True

    def stop(self) -> None:
        """Record stream stop."""
        self.stopped = True

    def handle_event(self, data: dict[str, Any]) -> None:
        """Record one rendered stream event."""
        self.events.append(data)


class _FakeAgent:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def astream(self, **kwargs: Any):
        """Yield a minimal DataAgent-compatible stream."""
        self.kwargs = kwargs
        yield ("custom", {"type": "llm_output", "payload": {"content": "hello"}})
        yield (
            "updates",
            {
                "messages": [{"role": "assistant", "content": "hello"}],
                "complete": True,
                "session_id": "session-1",
            },
        )


@pytest.mark.asyncio
async def test_stream_agent_response_renders_custom_events_and_returns_updates() -> None:
    """CLI stream helper should render custom chunks and return the final update payload."""
    agent = _FakeAgent()
    renderer = _FakeRenderer()

    response = await _stream_agent_response(
        agent,  # type: ignore[arg-type]
        user_query="hello",
        initial_state={"session_id": "session-1"},
        renderer=renderer,  # type: ignore[arg-type]
    )

    assert renderer.started is True
    assert renderer.stopped is True
    assert renderer.events == [{"type": "llm_output", "payload": {"content": "hello"}}]
    assert response["complete"] is True
    assert agent.kwargs["initial_state"]["user_query"] == "hello"


def test_stream_renderer_renders_tool_args_tree_and_content() -> None:
    """Tool call and result panels should show args and result content."""
    rich_console = pytest.importorskip("rich.console")
    console = rich_console.Console(record=True, width=100)
    renderer = StreamRenderer(console)

    renderer.render_tool_call(
        {
            "type": "tool_call",
            "payload": {
                "tool_name": "read_file",
                "tool_args": {"path": "/tmp/a.csv", "options": {"head": 10}},
            },
        }
    )
    renderer.render_tool_result(
        {
            "type": "tool_result",
            "payload": {
                "tool_name": "read_file",
                "tool_result": "### Result\n\nloaded",
                "tool_output": {"success": True, "content": "### Result\n\nloaded", "debug": "hidden"},
            },
        }
    )

    output = console.export_text()
    assert "path: /tmp/a.csv" in output
    assert "options" in output
    assert "head: 10" in output
    assert "loaded" in output
    assert "hidden" not in output


def test_stream_renderer_streams_native_llm_output_and_answer_fallback() -> None:
    """Renderer should stream llm_output in a Markdown panel and skip duplicate answer chunks."""
    rich_console = pytest.importorskip("rich.console")
    console = rich_console.Console(record=True, width=100)
    renderer = StreamRenderer(console)

    renderer.handle_event({"type": "llm_output", "payload": {"content": "hello"}})
    renderer.handle_event({"type": "answer", "payload": {"output": "hello"}})
    renderer.stop()

    output = console.export_text()
    assert output.count("hello") == 1
    assert "DataAgent" in output


def test_stream_renderer_renders_answer_when_no_llm_output() -> None:
    """Renderer should use answer chunks when no llm_output was received."""
    rich_console = pytest.importorskip("rich.console")
    console = rich_console.Console(record=True, width=100)
    renderer = StreamRenderer(console)

    renderer.handle_event({"type": "answer", "payload": {"output": "fallback"}})
    renderer.stop()

    assert "fallback" in console.export_text()


def test_stream_renderer_resets_reasoning_between_rounds() -> None:
    """Reasoning panels should show the current reasoning round, not all prior rounds."""
    rich_console = pytest.importorskip("rich.console")
    console = rich_console.Console(record=True, width=100)
    renderer = StreamRenderer(console)

    renderer.handle_event({"type": "llm_reasoning", "payload": {"content": "第一轮思考"}})
    renderer.handle_event({"type": "tool_call", "payload": {"tool_name": "list_files", "tool_args": {}}})
    renderer.handle_event({"type": "llm_reasoning", "payload": {"content": "第二轮思考"}})
    renderer.stop()

    output = console.export_text()
    assert "第二轮思考" in output
    latest_panel = output.rsplit("思考过程", maxsplit=1)[-1]
    assert "第一轮思考" not in latest_panel


def test_stream_renderer_starts_fresh_output_after_tool_round() -> None:
    """A new output round after tools should not include earlier assistant text."""
    rich_console = pytest.importorskip("rich.console")
    console = rich_console.Console(record=True, width=100)
    renderer = StreamRenderer(console)

    renderer.handle_event({"type": "llm_output", "payload": {"content": "先看看目录。"}})
    renderer.handle_event({"type": "tool_call", "payload": {"tool_name": "list_files", "tool_args": {}}})
    renderer.handle_event({"type": "llm_output", "payload": {"content": "# 最终总结\n\n完成。"}})
    renderer.stop()

    output = console.export_text()
    latest_panel = output.rsplit("DataAgent", maxsplit=1)[-1]
    assert "最终总结" in latest_panel
    assert "完成" in latest_panel
    assert "先看看目录" not in latest_panel
