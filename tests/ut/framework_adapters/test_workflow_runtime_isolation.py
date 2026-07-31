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
"""Regression tests for per-invocation LangGraph Runtime isolation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dataagent.core.cbb import BaseNode, BaseRouter, BaseState
from dataagent.core.cbb.agent_env import Env
from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.nodes.planner import Planner
from dataagent.core.framework_adapters.runtime.context import get_current_runtime
from dataagent.core.framework_adapters.runtime.workflow import LangGraphWorkflow
from dataagent.core.framework_adapters.runtime.workflow_backend import LangGraphWorkflowBackend


class _RuntimeProbeState(BaseState):
    """Minimal workflow state that records the Runtime observed by a node."""

    request_marker: str
    seen_context_runtime: str
    seen_runtime: str
    seen_state_marker: str


class _RuntimeProbeNode(BaseNode):
    """Record the marker from the Runtime supplied to this invocation."""

    def __init__(self) -> None:
        super().__init__(name="probe", chat_model_name=None)

    async def _aprocess(self, state: _RuntimeProbeState, runtime: Any = None) -> dict[str, Any]:
        """Return the marker on the Runtime visible to this node invocation."""
        await asyncio.sleep(0)
        current_runtime = get_current_runtime()
        return {
            "seen_context_runtime": getattr(current_runtime, "marker", ""),
            "seen_runtime": getattr(runtime, "marker", ""),
            "seen_state_marker": str(state.get("request_marker", "")),
        }


class _RuntimeProbeRouter(BaseRouter):
    """Route the single probe node directly to the graph end."""

    def __init__(self) -> None:
        super().__init__(entry_point="probe")
        self.add_custom_rule("probe", lambda _state: "__end__")


class _PlannerModelProbe:
    """Record the tools bound to one call's model."""

    def __init__(self) -> None:
        self.bound_tools: list[Any] = []

    def bind_tools(self, tools: list[Any]) -> _PlannerModelProbe:
        """Record tools and return the bound model."""
        self.bound_tools = list(tools)
        return self


class _PlannerRuntimeProbe:
    """Provide a distinct model and tool set for one Planner call."""

    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.model = _PlannerModelProbe()
        self.llm_calls: list[str] = []

    def llm(self, name: str) -> _PlannerModelProbe:
        """Return this invocation's model probe."""
        self.llm_calls.append(name)
        return self.model

    def get_tools_for_llm(self) -> list[str]:
        """Return this invocation's unique tool marker."""
        return [self.marker]


@pytest.mark.asyncio
async def test_concurrent_ainvoke_uses_its_own_runtime() -> None:
    """Concurrent invocations on one workflow must not read another call's Runtime."""
    workflow = LangGraphWorkflow(
        nodes=[_RuntimeProbeNode()],
        router=_RuntimeProbeRouter(),
        state_class=_RuntimeProbeState,
    )
    backend = LangGraphWorkflowBackend(workflow)
    runtime_a = SimpleNamespace(marker="runtime-a", max_iter=1)
    runtime_b = SimpleNamespace(marker="runtime-b", max_iter=1)

    result_a, result_b = await asyncio.gather(
        backend.ainvoke({"messages": [], "request_marker": "request-a"}, runtime=runtime_a),
        backend.ainvoke({"messages": [], "request_marker": "request-b"}, runtime=runtime_b),
    )

    assert result_a.get("seen_runtime") == "runtime-a"
    assert result_b.get("seen_runtime") == "runtime-b"
    assert result_a.get("seen_context_runtime") == "runtime-a"
    assert result_b.get("seen_context_runtime") == "runtime-b"
    assert result_a.get("seen_state_marker") == "request-a"
    assert result_b.get("seen_state_marker") == "request-b"


@pytest.mark.asyncio
async def test_concurrent_astream_uses_its_own_runtime() -> None:
    """Concurrent streams on one workflow must keep Runtime context isolated."""
    workflow = LangGraphWorkflow(
        nodes=[_RuntimeProbeNode()],
        router=_RuntimeProbeRouter(),
        state_class=_RuntimeProbeState,
    )
    backend = LangGraphWorkflowBackend(workflow)
    runtime_a = SimpleNamespace(marker="stream-a", max_iter=1)
    runtime_b = SimpleNamespace(marker="stream-b", max_iter=1)

    async def collect(runtime: Any) -> dict[str, Any]:
        """Collect the final values event for one Runtime."""
        final: dict[str, Any] = {}
        async for item in backend.astream({"messages": []}, runtime=runtime, stream_mode="values"):
            if isinstance(item, dict):
                final = item
        return final

    result_a, result_b = await asyncio.gather(collect(runtime_a), collect(runtime_b))

    assert result_a.get("seen_runtime") == "stream-a"
    assert result_b.get("seen_runtime") == "stream-b"
    assert result_a.get("seen_context_runtime") == "stream-a"
    assert result_b.get("seen_context_runtime") == "stream-b"


@pytest.mark.asyncio
async def test_planner_binds_llm_from_each_invocation_runtime() -> None:
    """A shared Planner node must not reuse a model bound for another Runtime."""
    planner = Planner(name="planner", chat_model="chat_model")
    runtime_a = _PlannerRuntimeProbe("tool-a")
    runtime_b = _PlannerRuntimeProbe("tool-b")
    state = {
        "intent_complete": False,
        "missing_slots": ["value"],
        "intent_missing_message": "missing",
    }

    await planner._aprocess(state, runtime_a)
    await planner._aprocess(state, runtime_b)

    assert runtime_a.llm_calls == ["planner"]
    assert runtime_b.llm_calls == ["planner"]
    assert runtime_a.model.bound_tools == ["tool-a"]
    assert runtime_b.model.bound_tools == ["tool-b"]


def test_runtime_workspace_is_not_stored_on_shared_env(tmp_path: Path) -> None:
    """Per-call Runtime workspaces must remain isolated when their Env is shared."""
    shared_env = Env(llm_configs={}, tavily_configs={}, modules={}, hooks={})
    runtime_a = Runtime(shared_env)
    runtime_b = Runtime(shared_env)
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"

    runtime_a.update_from_state({"workspace": workspace_a})
    runtime_b.update_from_state({"workspace": workspace_b})

    assert runtime_a.workspace_dir == workspace_a
    assert runtime_b.workspace_dir == workspace_b
