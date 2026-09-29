"""Regression test for streamable-HTTP MCP cancellation safety.

Background: commit 2a4a9599 made ``MCPClientWrapper._execute_with_connection``
reuse a single streamable-HTTP session across calls. That change was safe for
resource-job coordinators (which own a dedicated event loop), but it broke
the tool-call path that runs on the LangGraph pregel event loop: when an outer
``astream`` cancel scope tears down, the MCP ``BaseSession`` / ``post_writer``
tasks can be left blocked on ``MemoryObjectSendStream.send`` while their
counterpart receiver is already closed, and ``ClientSession.__aexit__`` blocks
waiting for those tasks to settle.

The fix splits ``_execute_with_connection``'s streamable-http branch into two
modes:

- ``reuse_streamable_http_session=False`` (default, used by tool calls such as
  the DataOps prod adapter): each call opens and tears down its own
  ``httpx.AsyncClient`` / ``streamable_http_client`` / ``ClientSession`` inside
  nested ``async with`` blocks. Cancelling the calling task must propagate
  cleanly without hanging.
- ``reuse_streamable_http_session=True`` (used only by resource-job
  coordinators that pin a dedicated loop): the previous shared-session behavior
  is preserved.

This test exercises the ``False`` path with a live mock MCP server, cancels
the host task while a request is in flight at the wire level, and asserts
that the ``CancelledError`` propagates within a small bounded time. Without
the fix, the cancellation hangs on the MCP internal
``MemoryObjectSendStream.send``.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest
from mcp import types as mcp_types

from dataagent.actions.tools.mcp import MCPClientWrapper, MCPServerConfig


def _free_port() -> int:
    """Return an unused TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_mock(url: str, *, timeout_sec: float = 20.0) -> None:
    """Block until the mock MCP initialize handshake succeeds."""
    deadline = time.monotonic() + timeout_sec
    payload = (
        b'{"jsonrpc":"2.0","method":"initialize","id":1,'
        b'"params":{"protocolVersion":"2025-03-26","capabilities":{},'
        b'"clientInfo":{"name":"ut","version":"0.1.0"}}}'
    )
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.2)
    raise RuntimeError(f"mock MCP did not become ready: {url}")


@pytest.fixture()
def mock_mcp_url() -> Iterator[str]:
    """Start mock_compute_pool and yield its streamable HTTP URL."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "dataagent.actions.tools.mcp_tool.mock_compute_pool",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_mock(url)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _build_call_tool_request(name: str, arguments: dict) -> mcp_types.ClientRequest:
    """Build a wire-level tools/call request without going through ClientSession.call_tool."""
    return mcp_types.ClientRequest(
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
        )
    )


def test_send_request_cancel_propagates_without_hanging(mock_mcp_url: str) -> None:
    """Cancelling the host task must surface CancelledError quickly on the
    non-reuse streamable_http MCPClientWrapper path.

    Why raw ``session.send_request`` rather than ``session.call_tool``:
        ``call_tool`` returns once the wire round-trip completes; if the
        mock answers in microseconds the host task is already done before
        ``task.cancel()`` runs and there is nothing to cancel. Driving the
        request through ``send_request`` directly and using the mock's
        ``wait_sec`` knob to keep the server-side handler parked, we
        guarantee the request is still in-flight (the client future has
        not yet been resolved) at the moment we cancel — which is the
        exact regression window from the pregel astream bug.

    Why an ``asyncio.Event`` between the operation and the cancel point:
        We use ``in_flight`` as a sync point so the cancel fires strictly
        after ``send_request`` has parked on the wire, not at some
        arbitrary wall-clock offset. This eliminates flake from CI noise
        and from any future mock server becoming faster.

    Why we do not assert on the inner exception type:
        ``BaseSession.send_request`` parks on an anyio
        ``MemoryObjectSendStream`` / receiver pair. When the surrounding
        task is cancelled, the outer anyio task-group teardown closes
        those streams, which causes ``receive()`` to raise ``EndOfStream``
        (or an anyio-wrapped equivalent) inside ``send_request``'s
        ``finally`` rather than re-raising ``CancelledError``. The contract
        we care about is that ``ClientSession.__aexit__`` does not block
        on stranded ``post_writer`` tasks, so the host task must
        propagate ``CancelledError`` within the deadline regardless of
        what the inner ``send_request`` future settles with.
    """
    config = MCPServerConfig(
        server_id="cancel-regression",
        transport_type="streamable_http",
        config={"url": mock_mcp_url, "timeout": 10},
    )
    client = MCPClientWrapper(config)  # reuse_streamable_http_session defaults to False

    # ``in_flight`` is set inside the operation *after* send_request has
    # already parked on the wire. The driver awaits it before issuing
    # ``task.cancel()`` so the cancel races against an in-flight request
    # rather than against ``__aenter__`` setup.
    in_flight: asyncio.Event = asyncio.Event()

    async def _send_in_flight_request(session) -> None:
        request = _build_call_tool_request(
            "poll_job",
            {"job_id": "remote-missing", "wait_sec": 5.0},
        )
        # Signal that we are about to park on the wire; then issue the
        # request which will block until the mock server's time.sleep
        # completes (5s, far longer than the cancel window).
        in_flight.set()
        await session.send_request(request, mcp_types.CallToolResult)

    async def _long_running_call() -> None:
        await client._execute_with_connection(_send_in_flight_request)

    async def _drive() -> None:
        task = asyncio.create_task(_long_running_call())
        # Wait until send_request has actually parked on the wire before
        # cancelling — no wall-clock guessing.
        await asyncio.wait_for(in_flight.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    deadline_sec = 5.0
    started = time.monotonic()
    asyncio.run(_drive())
    elapsed = time.monotonic() - started

    assert elapsed < deadline_sec, (
        f"CancelledError did not propagate within {deadline_sec}s; "
        f"MCP internal streams are likely stranded again. elapsed={elapsed:.2f}s"
    )


def test_non_reuse_connect_raises_with_actionable_message() -> None:
    """connect()/disconnect() must be refused on the non-reuse streamable_http path."""
    config = MCPServerConfig(
        server_id="non-reuse",
        transport_type="streamable_http",
        config={"url": "http://127.0.0.1:9/mcp", "timeout": 1},
    )
    client = MCPClientWrapper(config)  # reuse_streamable_http_session=False

    async def _drive() -> None:
        from dataagent.core.errors import DataAgentError

        with pytest.raises(DataAgentError) as excinfo:
            await client.connect()
        assert "reuse_streamable_http_session=True" in str(excinfo.value)
        # disconnect() is a no-op on this path, must not raise.
        await client.disconnect()

    asyncio.run(_drive())


def test_reuse_flag_round_trip_through_constructor() -> None:
    """Opt-in reuse=True keeps the constructor flag and refuses _ensure_session misuse.

    State-machine assertions only — we do not open a real socket here, because
    the long-lived session is established lazily by ``_ensure_streamable_http_session``
    inside a coordinator that owns a dedicated event loop, which is exercised
    by ``tests/ut/resources/test_resource_mcp_concurrent_jobs.py``.
    """
    config_default = MCPServerConfig(
        server_id="reuse-default",
        transport_type="streamable_http",
        config={"url": "http://127.0.0.1:9/mcp", "timeout": 1},
    )
    default_client = MCPClientWrapper(config_default)
    assert default_client._reuse_streamable_http_session is False

    config_reuse = MCPServerConfig(
        server_id="reuse-explicit",
        transport_type="streamable_http",
        config={"url": "http://127.0.0.1:9/mcp", "timeout": 1},
    )
    reuse_client = MCPClientWrapper(config_reuse, reuse_streamable_http_session=True)
    assert reuse_client._reuse_streamable_http_session is True

    async def _non_reuse_ensure_raises() -> None:
        from dataagent.core.errors import DataAgentError

        with pytest.raises(DataAgentError, match="_ensure_streamable_http_session"):
            await default_client._ensure_streamable_http_session()

    asyncio.run(_non_reuse_ensure_raises())
