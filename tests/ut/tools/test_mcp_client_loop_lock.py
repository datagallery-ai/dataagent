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
"""Unit tests for MCPClientWrapper event-loop-safe connection locking."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed

from dataagent.actions.tools.mcp import MCPClientWrapper, MCPServerConfig


def _build_http_client() -> MCPClientWrapper:
    """Build one streamable_http MCP client wrapper for lock tests."""
    config = MCPServerConfig(
        server_id="lock-test",
        transport_type="streamable_http",
        config={"url": "http://127.0.0.1:9/mcp", "timeout": 1},
    )
    return MCPClientWrapper(config)


def test_connection_lock_safe_across_concurrent_asyncio_run():
    """Shared client locks must not raise when used from concurrent event loops."""
    client = _build_http_client()

    async def hold_lock() -> str:
        """Acquire the per-loop connection lock briefly."""
        async with client._get_connection_lock():
            await asyncio.sleep(0.05)
            return "ok"

    def run_hold() -> str:
        """Run one lock hold inside a fresh event loop."""
        return asyncio.run(hold_lock())

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_hold) for _ in range(2)]
        results = [future.result(timeout=5) for future in as_completed(futures)]

    assert sorted(results) == ["ok", "ok"]
