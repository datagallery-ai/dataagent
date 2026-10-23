"""Both HTTP session policies must respect MCP's task-owned cancel scopes."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import pytest
from mcp.types import CallToolResult, TextContent

from dataagent.actions.tools import mcp
from dataagent.core.errors import DataAgentError


@pytest.fixture(params=[False, True], ids=["per-call", "reuse"])
def http_client(monkeypatch, request):
    state = SimpleNamespace(opened=[], closed=[], calls=[], initialize_modes=[], started=None)

    @asynccontextmanager
    async def connection(*args, **kwargs):
        owner = asyncio.current_task()
        async with anyio.create_task_group() as group:
            group.start_soon(anyio.sleep_forever)
            try:
                yield None, None, None
            finally:
                assert asyncio.current_task() is owner
                group.cancel_scope.cancel()

    class Session:
        async def initialize(self):
            mode = state.initialize_modes.pop(0) if state.initialize_modes else "ok"
            if mode == "hang":
                await anyio.sleep_forever()
            if mode == "error":
                raise RuntimeError("handshake failed")

        async def call_tool(self, name, arguments):
            state.calls.append(name)
            if name == "hang":
                state.started.set()
                await anyio.sleep_forever()
            return CallToolResult(content=[TextContent(type="text", text="ok")])

    @asynccontextmanager
    async def session(*args):
        owner = asyncio.current_task()
        state.opened.append(owner)
        async with anyio.create_task_group() as group:
            group.start_soon(anyio.sleep_forever)
            try:
                yield Session()
            finally:
                state.closed.append(asyncio.current_task())
                assert asyncio.current_task() is owner
                group.cancel_scope.cancel()

    monkeypatch.setattr(mcp, "streamable_http_client", connection)
    monkeypatch.setattr(mcp, "ClientSession", session)
    client = mcp.MCPClientWrapper(
        mcp.MCPServerConfig(
            "lifecycle-test", "streamable_http", {"url": "http://127.0.0.1:9/mcp", "operation_timeout": 0.5}
        ),
        reuse_streamable_http_session=request.param,
    )
    return client, state


def test_session_policy_across_request_tasks_and_cleanup_by_owner(http_client):
    client, state = http_client

    async def check():
        results = await asyncio.gather(*(client.call_tool("healthy", {}) for _ in range(5)))
        assert all(result.content[0].text == "ok" for result in results)
        if client._reuse_streamable_http_session:
            assert len(state.opened) == 1
            assert not state.closed
        else:
            assert len(state.opened) == 5
            assert state.closed == state.opened
        await asyncio.create_task(client.disconnect())
        assert state.closed == state.opened
        assert not client._http_sessions

    asyncio.run(check())


@pytest.mark.parametrize("mode, message", [("hang", "timed out"), ("error", "handshake failed")])
def test_failed_handshake_closes_partial_session_and_next_call_reconnects(http_client, mode, message):
    client, state = http_client
    state.initialize_modes = [mode, "ok"]

    async def check():
        with pytest.raises(DataAgentError, match=message):
            await client.call_tool("first", {})
        assert state.closed == state.opened
        assert not state.calls
        result = await client.call_tool("healthy", {})
        assert result.content[0].text == "ok"
        assert len(state.opened) == 2
        await client.disconnect()
        assert state.closed == state.opened

    asyncio.run(check())


def test_external_cancellation_closes_session_without_retrying_tool(http_client):
    client, state = http_client

    async def check():
        state.started = asyncio.Event()
        request = asyncio.create_task(client.call_tool("hang", {}))
        await state.started.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert state.closed == state.opened
        assert state.calls == ["hang"]
        result = await client.call_tool("healthy", {})
        assert result.content[0].text == "ok"
        await client.disconnect()

    asyncio.run(check())


def test_shared_wrapper_uses_independent_sessions_for_concurrent_event_loops(http_client):
    client, state = http_client

    def call_from_thread():
        return asyncio.run(client.call_tool("healthy", {}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: call_from_thread(), range(2)))
    assert all(result.content[0].text == "ok" for result in results)
    assert len(state.opened) == len(state.closed) == 2
    assert set(state.opened) == set(state.closed)
    assert not client._http_sessions
