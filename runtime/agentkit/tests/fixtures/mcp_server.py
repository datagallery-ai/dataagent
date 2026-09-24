"""Local deterministic MCP peer; never talks to external services."""

import asyncio
import os
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

server = FastMCP("dataagent-test", host="127.0.0.1", port=int(sys.argv[2]) if len(sys.argv) > 2 else 8000)
calls = 0
if filename := os.environ.get("MCP_TEST_PIDS"):
    with Path(filename).open("a") as handle:
        handle.write(f"{os.getpid()}\n")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    global calls
    calls += 1
    return a + b


@server.tool()
def context() -> dict:
    """Report only synthetic test context, never host credentials."""
    return {"cwd": str(Path.cwd()), "test_value": os.environ.get("MCP_TEST_VALUE"), "calls": calls}


@server.tool()
def fail() -> str:
    """Return a native MCP domain error."""
    raise ValueError("synthetic MCP tool failure")


@server.tool()
async def slow() -> str:
    """Wait long enough for the test client to cancel."""
    if filename := os.environ.get("MCP_TEST_TOOL_STARTED"):
        Path(filename).touch()
    await asyncio.sleep(30)
    return "unexpected completion"


@server.tool()
def disconnect() -> str:
    """Terminate this test MCP process to exercise transport failure."""
    os._exit(2)


if __name__ == "__main__":
    time.sleep(float(os.environ.get("MCP_TEST_START_DELAY", "0")))
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if transport == "stdio":
        server.run(transport="stdio")
    else:
        import uvicorn

        app = server.streamable_http_app() if transport == "http" else server.sse_app()

        class TestHeader:
            def __init__(self, wrapped):
                self.wrapped = wrapped

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http" and dict(scope["headers"]).get(b"authorization") != b"Bearer test-mcp-token":
                    await send({"type": "http.response.start", "status": 401, "headers": []})
                    await send({"type": "http.response.body", "body": b"missing test header"})
                    return
                await self.wrapped(scope, receive, send)

        uvicorn.run(TestHeader(app), host="127.0.0.1", port=server.settings.port, log_level="error")
