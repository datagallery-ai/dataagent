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
"""Long-running tool functions for concurrency e2e tests.

Each tool sleeps to simulate real work, making concurrency overlap observable.
Two agents each get a disjoint set of tools — if there is any singleton leakage,
one agent would be able to call the other's tool (and fail the test).
"""

import asyncio
import time


def slow_tool_agent_a(query: str) -> str:
    """Agent A's exclusive tool — simulates a slow data fetch (3s).

    Returns a signed result so we can verify the caller was truly Agent A.
    LocalToolWrapper wraps this in ToolResult(success=True, data=result).
    """
    time.sleep(15)
    return f"[AGENT_A_TOOL] processed query '{query}'"


async def slow_tool_agent_b(query: str) -> str:
    """Agent B's exclusive tool — simulates a slow async computation (3s).

    Returns a signed result so we can verify the caller was truly Agent B.
    LocalToolWrapper wraps this in ToolResult(success=True, data=result).
    """
    await asyncio.sleep(30)
    return f"[AGENT_B_TOOL] processed query '{query}'"
