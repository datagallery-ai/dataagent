"""Native local-tool context injection tests."""

from __future__ import annotations

from typing import Any

import pytest
from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.config import ConfigManager
from dataagent.core.deepagents.config.tools import LocalToolConfigCompiler


def read_context_value(query: str, *, _tool_context: ToolExecutionContext) -> str:
    """Return config values injected by the native local-tool compiler."""
    tool_value = _tool_context.tool_config.get("label", "")
    database = _tool_context.config_manager.get("DATABASE.db_id", "")
    return f"{query}|{database}|{tool_value}|{_tool_context.runtime.tool_call_id}"


async def read_context_value_async(query: str, *, _tool_context: ToolExecutionContext) -> str:
    """Return a context value from an asynchronous local function."""
    return read_context_value(query, _tool_context=_tool_context)


def _compile_tool(function: str) -> BaseTool:
    raw_config: dict[str, Any] = {
        "DATABASE": {"db_id": "sales"},
        "TOOLS": {
            "local_functions": [
                {
                    "module": "tests.ut.tools.test_tool_execution_context",
                    "function": function,
                    "config": {"label": "native"},
                }
            ]
        },
    }
    config_manager = ConfigManager()
    config_manager.update(raw_config)
    return LocalToolConfigCompiler(raw_config, config_manager=config_manager).compile()[0]


def _runtime(tool: BaseTool) -> ToolRuntime:
    return ToolRuntime(
        state={"messages": []},
        context=None,
        config={"configurable": {"thread_id": "test"}},
        stream_writer=lambda _: None,
        tool_call_id="call-1",
        store=None,
        tools=[tool],
    )


def test_sync_local_tool_receives_native_runtime_and_compatible_config() -> None:
    """A sync local function receives config without exposing `_tool_context` to the model."""
    tool = _compile_tool("read_context_value")

    assert "_tool_context" not in tool.args
    assert tool.func is not None
    assert tool.func(query="metrics", runtime=_runtime(tool)) == "metrics|sales|native|call-1"


@pytest.mark.asyncio
async def test_async_local_tool_receives_native_runtime_and_compatible_config() -> None:
    """An async local function uses the same native context bridge."""
    tool = _compile_tool("read_context_value_async")

    assert tool.coroutine is not None
    assert await tool.coroutine(query="metrics", runtime=_runtime(tool)) == "metrics|sales|native|call-1"
