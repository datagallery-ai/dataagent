"""A live transport must not keep an unanswered MCP request alive forever."""

import asyncio
import time
from contextlib import asynccontextmanager

import anyio
import pytest
from mcp.types import CallToolResult, TextContent

from dataagent.actions.tools import mcp
from dataagent.core.errors import DataAgentError


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True, "60"])
def test_operation_timeout_requires_finite_positive_seconds(value):
    with pytest.raises(ValueError, match="operation_timeout"):
        mcp.MCPServerConfig("test", config={"command": "unused", "operation_timeout": value})


@pytest.mark.parametrize(
    ("transport", "reuse_session"),
    [("stdio", False), ("sse", False), ("streamable_http", False), ("streamable_http", True)],
)
def test_deadline_cancels_live_transport_and_releases_lock(monkeypatch, transport, reuse_session):
    closed = []

    @asynccontextmanager
    async def connection(*args, **kwargs):
        async with anyio.create_task_group() as group:
            group.start_soon(anyio.sleep_forever)
            try:
                yield (None, None, None) if transport == "streamable_http" else (None, None)
            finally:
                closed.append(True)
                group.cancel_scope.cancel()

    class Session:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def initialize(self):
            pass

        async def call_tool(self, name, arguments):
            if name == "hang":
                await anyio.sleep_forever()
            return CallToolResult(content=[TextContent(type="text", text="ok")])

    monkeypatch.setattr(mcp, "stdio_client", connection)
    monkeypatch.setattr(mcp, "sse_client", connection)
    monkeypatch.setattr(mcp, "streamable_http_client", connection)
    monkeypatch.setattr(mcp, "ClientSession", Session)
    config = {"url": "http://127.0.0.1:9/mcp", "command": "unused", "operation_timeout": 0.2}
    client = mcp.MCPClientWrapper(
        mcp.MCPServerConfig("deadline-test", transport, config), reuse_streamable_http_session=reuse_session
    )

    async def check():
        started = time.monotonic()
        with pytest.raises(DataAgentError, match="hang.*deadline-test.*timed out after 0.2s"):
            await client.call_tool("hang", {})
        assert time.monotonic() - started < 2
        assert closed and not client._get_connection_lock().locked()
        # The same wrapper is reusable; the cancelled connection cannot poison it.
        result = await client.call_tool("healthy", {})
        assert result.content[0].text == "ok"

    asyncio.run(check())


def test_deadline_includes_waiting_for_busy_client_lock():
    client = mcp.MCPClientWrapper(
        mcp.MCPServerConfig(
            "busy",
            "stdio",
            {"command": "unused", "operation_timeout": 0.02},
        )
    )

    async def check():
        async with client._get_connection_lock():
            with pytest.raises(DataAgentError, match="list tools.*busy.*timed out"):
                await client.list_tools()

    asyncio.run(check())
