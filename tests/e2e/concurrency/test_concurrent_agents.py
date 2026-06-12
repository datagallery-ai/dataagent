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
"""E2E concurrency test: two agents with different YAML configs run concurrently in the same process.

Verifies that per-Agent ToolManager isolation prevents cross-Agent tool leakage.
Each agent has its own exclusive tool with different sleep durations (15s sync + 30s async),
so concurrent execution completes in ~max(sleeps) vs serial sum, proving they don't block each other.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.managers.llm_manager.adapters import LLMResponse, LLMStreamChunk
from dataagent.interface.sdk.agent import DataAgent


class _PerAgentFakeChatModel:
    """Fake chat model that returns a specific tool call on turn 1, then a final answer."""

    def __init__(self, tool_name: str, tool_query: str, final_answer: str) -> None:
        self._tool_name = tool_name
        self._tool_query = tool_query
        self._final_answer = final_answer
        self._turn = 0

    def bind_tools(self, _tools: Any, **_kwargs: Any) -> _PerAgentFakeChatModel:
        return self

    async def astream(self, _chat_input: Any, **_kwargs: Any):
        self._turn += 1
        if self._turn == 1:
            final = LLMResponse(
                content="",
                usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                tool_calls=[{"id": "call-1", "name": self._tool_name, "args": {"query": self._tool_query}}],
            )
        else:
            final = LLMResponse(
                content=self._final_answer,
                usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )
        yield LLMStreamChunk(done=True, final_response=final)


@pytest.mark.asyncio
async def test_two_agents_concurrent_chat_no_tool_leakage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Two DataAgents with disjoint tools run async chat concurrently — no cross-contamination.

    - Agent A: only slow_tool_agent_a (15s sync sleep, run via asyncio.to_thread)
    - Agent B: only slow_tool_agent_b (30s async sleep)
    - Fake LLMs force each agent to call its own tool on turn 1
    - Both chats run via asyncio.gather(); elapsed ≈ max(15,30) << serial sum(45)
    """
    # ── env vars for LLM provider resolution during from_config() ──
    monkeypatch.setenv("BAILIAN_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("BAILIAN_API_KEY", "test-key")
    # ── disable context persistence ──
    from dataagent.core.context.context import Context

    monkeypatch.setattr(Context, "persist_to_json", lambda _self: None, raising=True)
    monkeypatch.setattr(Context, "persist_meta_to_json", lambda _self: None, raising=True)

    # ── per-agent fake LLMs ──
    fake_a = _PerAgentFakeChatModel(
        tool_name="slow_tool_agent_a",
        tool_query="hello from A",
        final_answer="Agent A done.",
    )
    fake_b = _PerAgentFakeChatModel(
        tool_name="slow_tool_agent_b",
        tool_query="hello from B",
        final_answer="Agent B done.",
    )

    # ── monkeypatch Runtime.llm to return the correct fake per agent ──
    _original_llm = Runtime.llm

    def _fake_runtime_llm(self: Runtime, name: str) -> Any:
        tools = self.list_tools()
        if "slow_tool_agent_a" in tools:
            return fake_a
        if "slow_tool_agent_b" in tools:
            return fake_b
        return _original_llm(self, name)

    monkeypatch.setattr(Runtime, "llm", _fake_runtime_llm, raising=True)

    # ── load agents from YAML configs ──
    config_dir = Path(__file__).resolve().parent
    agent_a = DataAgent.from_config(str(config_dir / "agent_a.yaml"))
    agent_b = DataAgent.from_config(str(config_dir / "agent_b.yaml"))

    # ── pre-flight: verify tool isolation ──
    tools_a = set(agent_a._chat_agent.env_config.tool_manager.list_tools())
    tools_b = set(agent_b._chat_agent.env_config.tool_manager.list_tools())
    assert "slow_tool_agent_a" in tools_a
    assert "slow_tool_agent_b" not in tools_a, "Agent A should NOT see Agent B's tool"
    assert "slow_tool_agent_b" in tools_b
    assert "slow_tool_agent_a" not in tools_b, "Agent B should NOT see Agent A's tool"

    workspace_a = tmp_path / "agent_a_ws"
    workspace_b = tmp_path / "agent_b_ws"
    workspace_a.mkdir(parents=True, exist_ok=True)
    workspace_b.mkdir(parents=True, exist_ok=True)

    async def _run_agent(agent: DataAgent, workspace: Path) -> dict[str, Any]:
        state: dict[str, Any] = {
            "run_id": 0,
            "sub_id": 0,
            "workspace": str(workspace),
        }
        return await agent.chat("process this", initial_state=state)

    # ── run both concurrently ──
    t0 = time.monotonic()
    result_a, result_b = await asyncio.gather(
        _run_agent(agent_a, workspace_a),
        _run_agent(agent_b, workspace_b),
    )
    elapsed = time.monotonic() - t0

    # ── verify both completed ──
    assert isinstance(result_a, dict), f"Agent A result type: {type(result_a)}"
    assert isinstance(result_b, dict), f"Agent B result type: {type(result_b)}"
    assert result_a.get("complete") is True, f"Agent A did not complete: {result_a.get('error', 'unknown')}"
    assert result_b.get("complete") is True, f"Agent B did not complete: {result_b.get('error', 'unknown')}"

    # ── verify each agent called its own tool (2 turns: 1 tool + 1 final) ──
    assert result_a.get("num_turns") == 2, f"Agent A turns: {result_a.get('num_turns')}"
    assert result_b.get("num_turns") == 2, f"Agent B turns: {result_b.get('num_turns')}"
    assert result_a.get("num_valid_tool_calls") == 1
    assert result_b.get("num_valid_tool_calls") == 1

    # ── verify tool results contain the signed agent marker ──
    messages_a = result_a.get("messages", [])
    messages_b = result_b.get("messages", [])
    tool_results_a = [str(m) for m in messages_a if getattr(m, "type", None) == "tool"]
    tool_results_b = [str(m) for m in messages_b if getattr(m, "type", None) == "tool"]
    assert any("AGENT_A_TOOL" in r for r in tool_results_a), f"Agent A results: {tool_results_a}"
    assert any("AGENT_B_TOOL" in r for r in tool_results_b), f"Agent B results: {tool_results_b}"

    # ── verify actual concurrency: elapsed ≈ max(15,30) << serial sum(45s) ──
    # Allow margin for overhead, but must be well under 45s
    assert elapsed < 35.0, (
        f"Concurrency check failed: elapsed={elapsed:.1f}s, "
        f"expected <35.0s (serial would be >45s). Tools did NOT run concurrently."
    )
