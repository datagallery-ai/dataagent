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
import asyncio
import queue
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

import dataagent.core.framework_adapters.runtime.workflow_openjiuwen as workflow_openjiuwen_module
from dataagent.core.framework_adapters.runtime.context import get_stream_writer
from dataagent.core.framework_adapters.runtime.workflow_backend import OpenJiuWenWorkflowBackend
from dataagent.core.framework_adapters.runtime.workflow_openjiuwen import OpenJiuWenWorkflow


def test_merge_delta_consumes_remove_all_message() -> None:
    workflow = OpenJiuWenWorkflow(nodes=[], router=None)
    old_message = HumanMessage(content="old")
    summary_message = HumanMessage(content="summary")
    latest_message = AIMessage(content="latest")

    merged = workflow._merge_delta(
        {"messages": [old_message]},
        {"messages": [RemoveMessage(id="__remove_all__"), summary_message, latest_message]},
    )

    assert merged["messages"] == [summary_message, latest_message]


def test_merge_delta_appends_regular_message_delta() -> None:
    workflow = OpenJiuWenWorkflow(nodes=[], router=None)
    old_message = HumanMessage(content="old")
    new_message = AIMessage(content="new")

    merged = workflow._merge_delta({"messages": [old_message]}, {"messages": [new_message]})

    assert merged["messages"] == [old_message, new_message]


@pytest.mark.asyncio
async def test_backend_keeps_agent_runtime_separate_from_workflow_runtime() -> None:
    agent_runtime = object()
    workflow_runtime = object()

    class _WorkflowStub:
        async def ainvoke(self, initial_state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            assert kwargs.get("agent_runtime") is agent_runtime
            assert kwargs.get("runtime") is workflow_runtime
            return initial_state

    backend = OpenJiuWenWorkflowBackend(_WorkflowStub())

    result = await backend.ainvoke(
        {"complete": True},
        runtime=agent_runtime,
        workflow_runtime=workflow_runtime,
    )

    assert result.get("complete") is True


@pytest.mark.asyncio
async def test_stream_backend_keeps_agent_runtime_separate_from_workflow_runtime() -> None:
    agent_runtime = object()
    workflow_runtime = object()

    class _WorkflowStub:
        async def astream(self, initial_state: dict[str, Any], **kwargs: Any):
            assert kwargs.get("agent_runtime") is agent_runtime
            assert kwargs.get("runtime") is workflow_runtime
            yield initial_state

    backend = OpenJiuWenWorkflowBackend(_WorkflowStub())
    stream = backend.astream(
        {"complete": True},
        runtime=agent_runtime,
        workflow_runtime=workflow_runtime,
    )

    results = [item async for item in stream]

    assert results == [{"complete": True}]


@pytest.mark.asyncio
async def test_resume_backend_keeps_agent_runtime_separate_from_workflow_runtime() -> None:
    agent_runtime = object()
    workflow_runtime = object()

    class _WorkflowStub:
        def load_checkpoint_state(self, checkpoint_id: str) -> tuple[str, dict[str, Any]]:
            assert checkpoint_id == "checkpoint"
            return "human_feedback", {}

        async def ainvoke(self, initial_state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            assert initial_state.get("__human_feedback_resume__") == "continue"
            assert kwargs.get("agent_runtime") is agent_runtime
            assert kwargs.get("runtime") is workflow_runtime
            return initial_state

    backend = OpenJiuWenWorkflowBackend(_WorkflowStub())

    result = await backend.resume(
        checkpoint_id="checkpoint",
        message="continue",
        runtime=agent_runtime,
        workflow_runtime=workflow_runtime,
    )

    assert result.get("__human_feedback_resume__") == "continue"


@pytest.mark.asyncio
async def test_stream_resume_backend_keeps_agent_runtime_separate_from_workflow_runtime() -> None:
    agent_runtime = object()
    workflow_runtime = object()

    class _WorkflowStub:
        def load_checkpoint_state(self, checkpoint_id: str) -> tuple[str, dict[str, Any]]:
            assert checkpoint_id == "checkpoint"
            return "human_feedback", {}

        async def astream(self, initial_state: dict[str, Any], **kwargs: Any):
            assert initial_state.get("__human_feedback_resume__") == "continue"
            assert kwargs.get("agent_runtime") is agent_runtime
            assert kwargs.get("runtime") is workflow_runtime
            yield initial_state

    backend = OpenJiuWenWorkflowBackend(_WorkflowStub())
    stream = backend.astream_resume(
        checkpoint_id="checkpoint",
        message="continue",
        runtime=agent_runtime,
        workflow_runtime=workflow_runtime,
    )

    results = [item async for item in stream]

    assert results[0].get("__human_feedback_resume__") == "continue"


@pytest.mark.asyncio
async def test_compatibility_runtime_default_is_isolated_between_tasks() -> None:
    workflow = OpenJiuWenWorkflow(nodes=[], router=None)
    first_runtime = object()
    second_runtime = object()
    both_bound = asyncio.Event()
    bound_count = 0

    async def _bind_and_resolve(runtime: Any) -> Any:
        nonlocal bound_count
        workflow.set_runtime(runtime)
        bound_count += 1
        if bound_count == 2:
            both_bound.set()
        await both_bound.wait()
        return workflow._resolve_agent_runtime(None)

    first_result, second_result = await asyncio.gather(
        _bind_and_resolve(first_runtime),
        _bind_and_resolve(second_runtime),
    )

    assert first_result is first_runtime
    assert second_result is second_runtime


def test_call_context_is_bound_to_each_workflow_runtime() -> None:
    workflow = OpenJiuWenWorkflow(nodes=[], router=None)

    class _WorkflowRuntime:
        pass

    first_workflow_runtime = _WorkflowRuntime()
    second_workflow_runtime = _WorkflowRuntime()
    first_agent_runtime = object()
    second_agent_runtime = object()
    first_queue: queue.Queue[dict[str, Any]] = queue.Queue()
    second_queue: queue.Queue[dict[str, Any]] = queue.Queue()

    workflow._bind_call_context(first_workflow_runtime, first_agent_runtime, first_queue)
    workflow._bind_call_context(second_workflow_runtime, second_agent_runtime, second_queue)

    first_context = workflow._get_call_context(first_workflow_runtime)
    second_context = workflow._get_call_context(second_workflow_runtime)

    assert first_context is not None
    assert first_context.agent_runtime is first_agent_runtime
    assert first_context.stream_queue is first_queue
    assert second_context is not None
    assert second_context.agent_runtime is second_agent_runtime
    assert second_context.stream_queue is second_queue


def test_call_context_is_resolved_through_openjiuwen_session_wrappers() -> None:
    workflow = OpenJiuWenWorkflow(nodes=[], router=None)

    class _WorkflowRuntime:
        pass

    class _NodeSession:
        def __init__(self, session: _WorkflowRuntime) -> None:
            self._session = session

    class _ComponentSession:
        def __init__(self, inner: _NodeSession) -> None:
            self._inner = inner

    workflow_runtime = _WorkflowRuntime()
    component_session = _ComponentSession(_NodeSession(workflow_runtime))
    agent_runtime = object()
    workflow._bind_call_context(workflow_runtime, agent_runtime)

    call_context = workflow._get_call_context(component_session)

    assert call_context is not None
    assert call_context.agent_runtime is agent_runtime


@pytest.mark.asyncio
async def test_concurrent_nodes_use_agent_runtime_bound_to_their_workflow_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = OpenJiuWenWorkflow(nodes=[], router=None)

    class _WorkflowRuntime:
        @staticmethod
        def get_global_state(_key: str) -> Any:
            return None

    first_workflow_runtime = _WorkflowRuntime()
    second_workflow_runtime = _WorkflowRuntime()
    first_agent_runtime = object()
    second_agent_runtime = object()
    workflow._bind_call_context(first_workflow_runtime, first_agent_runtime)
    workflow._bind_call_context(second_workflow_runtime, second_agent_runtime)
    workflow.set_runtime(object())
    observed: dict[str, Any] = {}
    both_running = asyncio.Event()

    async def _execute_node(
        node: Any,
        state_for_node: Any,
        node_name: str,
        runtime: Any,
    ) -> dict[str, Any]:
        _ = state_for_node
        observed[node_name] = runtime
        if len(observed) == 2:
            both_running.set()
        await both_running.wait()
        return {}

    monkeypatch.setattr(workflow, "_ojw_execute_node_with_interrupt", _execute_node)
    monkeypatch.setattr(workflow, "_ojw_merge_and_commit_delta", lambda *args: {})

    await asyncio.gather(
        workflow.invoke_node_component(
            SimpleNamespace(name="first"),
            first_workflow_runtime,
            lambda _runtime: {},
            lambda _current, _delta: {},
        ),
        workflow.invoke_node_component(
            SimpleNamespace(name="second"),
            second_workflow_runtime,
            lambda _runtime: {},
            lambda _current, _delta: {},
        ),
    )

    assert observed.get("first") is first_agent_runtime
    assert observed.get("second") is second_agent_runtime


@pytest.mark.asyncio
async def test_concurrent_nodes_write_to_their_own_stream_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = OpenJiuWenWorkflow(nodes=[], router=None)

    class _WorkflowRuntime:
        @staticmethod
        def get_global_state(_key: str) -> Any:
            return None

        @staticmethod
        def state() -> Any:
            return None

    first_workflow_runtime = _WorkflowRuntime()
    second_workflow_runtime = _WorkflowRuntime()
    first_queue: queue.Queue[dict[str, Any]] = queue.Queue()
    second_queue: queue.Queue[dict[str, Any]] = queue.Queue()
    workflow._bind_call_context(first_workflow_runtime, object(), first_queue)
    workflow._bind_call_context(second_workflow_runtime, object(), second_queue)
    both_running = asyncio.Event()
    running_count = 0

    async def _execute_node(
        node: Any,
        state_for_node: Any,
        node_name: str,
        runtime: Any,
    ) -> dict[str, Any]:
        nonlocal running_count
        _ = (state_for_node, runtime)
        running_count += 1
        get_stream_writer()({"node": node_name})
        if running_count == 2:
            both_running.set()
        await both_running.wait()
        return {}

    monkeypatch.setattr(workflow, "_ojw_execute_node_with_interrupt", _execute_node)
    monkeypatch.setattr(workflow, "_ojw_merge_and_commit_delta", lambda *args: {})

    await asyncio.gather(
        workflow.invoke_node_component(
            SimpleNamespace(name="first"),
            first_workflow_runtime,
            lambda _runtime: {},
            lambda _current, _delta: {},
        ),
        workflow.invoke_node_component(
            SimpleNamespace(name="second"),
            second_workflow_runtime,
            lambda _runtime: {},
            lambda _current, _delta: {},
        ),
    )

    assert first_queue.get_nowait().get("node") == "first"
    assert second_queue.get_nowait().get("node") == "second"


@pytest.mark.asyncio
async def test_astream_passes_call_context_without_workflow_instance_state(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = OpenJiuWenWorkflow(nodes=[], router=None)
    workflow_runtime = object()
    agent_runtime = object()

    async def _ainvoke(initial_state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        assert kwargs.get("agent_runtime") is agent_runtime
        stream_queue = kwargs.get("stream_queue")
        assert isinstance(stream_queue, queue.Queue)
        stream_queue.put({"content": "progress"})
        return initial_state

    monkeypatch.setattr(workflow, "ainvoke", _ainvoke)
    monkeypatch.setattr(workflow_openjiuwen_module, "_resolve_ojw_workflow_runtime_class", lambda: object)

    stream = workflow.astream(
        {"complete": True},
        runtime=workflow_runtime,
        agent_runtime=agent_runtime,
    )
    results = [item async for item in stream]

    assert results[0] == ("custom", {"content": "progress"})
    assert results[-1] == ("updates", {"complete": True})
    assert not hasattr(workflow, "_active_stream_queue")


def test_build_graph_returns_a_fresh_workflow_without_instance_state(monkeypatch: pytest.MonkeyPatch) -> None:
    class _WorkflowComponent:
        pass

    class _ComponentExecutable:
        pass

    class _End:
        pass

    class _Workflow:
        def add_workflow_comp(self, name: str, component: Any) -> None:
            _ = (name, component)

        def set_start_comp(self, name: str, component: Any) -> None:
            _ = (name, component)

        def set_end_comp(self, name: str, component: Any) -> None:
            _ = (name, component)

        def add_conditional_connection(self, name: str, route: Any) -> None:
            _ = (name, route)

    node = SimpleNamespace(name="planner")
    router = SimpleNamespace(entry_point="planner", routing_rules={"planner": lambda _state: "__end__"})
    workflow = OpenJiuWenWorkflow(nodes=[node], router=router)
    monkeypatch.setattr(
        workflow_openjiuwen_module,
        "_resolve_ojw_workflow_types",
        lambda: (_WorkflowComponent, _ComponentExecutable, _End, _Workflow, "__end__"),
    )

    first = workflow._build_graph(start_at="planner")
    second = workflow._build_graph(start_at="planner")

    assert isinstance(first, _Workflow)
    assert isinstance(second, _Workflow)
    assert first is not second
    assert not hasattr(workflow, "workflow")


@pytest.mark.asyncio
async def test_ainvoke_binds_and_releases_only_its_own_call_context(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = OpenJiuWenWorkflow(nodes=[], router=SimpleNamespace(entry_point="planner"))
    agent_runtime = object()
    stream_queue: queue.Queue[dict[str, Any]] = queue.Queue()
    reset_workflows: list[Any] = []

    class _State:
        def commit_user_inputs(self, initial_state: dict[str, Any]) -> None:
            _ = initial_state

    class _WorkflowRuntime:
        def state(self) -> _State:
            return _State()

        def context(self) -> dict[str, Any]:
            return {}

        async def close(self) -> None:
            return None

    class _Workflow:
        pass

    class _CompiledGraph:
        def __init__(self, built_workflow: _Workflow):
            self.built_workflow = built_workflow

        async def invoke(self, inputs: dict[str, Any], runtime: _WorkflowRuntime) -> None:
            _ = inputs
            call_context = workflow._get_call_context(runtime)
            assert call_context is not None
            assert call_context.agent_runtime is agent_runtime
            assert call_context.stream_queue is stream_queue

    workflow_runtime = _WorkflowRuntime()
    monkeypatch.setattr(workflow_openjiuwen_module, "_resolve_ojw_inputs_and_config_keys", lambda: ("input", "config"))
    monkeypatch.setattr(workflow_openjiuwen_module, "_resolve_ojw_workflow_runtime_class", lambda: _WorkflowRuntime)
    monkeypatch.setattr(workflow, "_build_graph", lambda *, start_at: _Workflow())
    monkeypatch.setattr(
        workflow_openjiuwen_module,
        "_compile_workflow_internal",
        lambda built_workflow, runtime: _CompiledGraph(built_workflow),
    )
    monkeypatch.setattr(workflow, "_ojw_reset_global_state", lambda runtime, state: None)
    monkeypatch.setattr(workflow, "_finalize_workflow_result", lambda runtime: {"complete": True})

    async def _reset_workflow(built_workflow: Any) -> None:
        reset_workflows.append(built_workflow)

    monkeypatch.setattr(workflow_openjiuwen_module, "_reset_workflow_internal", _reset_workflow)

    result = await workflow.ainvoke(
        {"conversation_id": "session"},
        runtime=workflow_runtime,
        agent_runtime=agent_runtime,
        stream_queue=stream_queue,
    )

    assert result.get("complete") is True
    assert len(reset_workflows) == 1
    assert isinstance(reset_workflows[0], _Workflow)
