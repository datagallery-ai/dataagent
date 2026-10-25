"""Closing an SSE consumer must close its engine before releasing the session."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from dataagent.core.flex.agent import FlexAgent
from dataagent.core.framework_adapters.runtime.workflow import LangGraphWorkflow
from dataagent.interface.rest_api.app import stream_agent_events
from dataagent.interface.rest_api.service import DataAgentService
from dataagent.interface.sdk.agent import DataAgent


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_during_read", [False, True])
async def test_sse_close_finishes_engine_before_releasing_workspace(monkeypatch, cancel_during_read):
    order = []
    waiting = asyncio.Event()

    async def engine_stream(**_kwargs):
        try:
            yield "working"
            waiting.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            order.append("engine_closed")

    agent = object.__new__(DataAgent)
    agent.type = "react"
    agent._chat_agent_instance = SimpleNamespace(astream=engine_stream)
    agent._initialize_state = lambda *_args: {"session_id": "close-test"}
    agent._ensure_workspace = lambda _state: None
    agent._touch_workspace_catalog = lambda _state: None
    agent._dump_runtime_config = lambda _state: None
    agent._acquire_session_workspace_lock = lambda _state: "lock"
    agent._release_session_workspace_lock = lambda _lock: order.append("lock_released")
    monkeypatch.setattr("dataagent.interface.sdk.agent.setup_session_log", lambda **_kwargs: None)

    @contextmanager
    def request_scope():
        try:
            yield {}
        finally:
            order.append("request_released")

    service = DataAgentService()
    service._agent = agent
    service._request_scope = request_scope
    stream = stream_agent_events("query", service)
    if cancel_during_read:
        async def consume():
            assert "event: message" in await anext(stream)
            await anext(stream)

        reader = asyncio.create_task(consume())
        await waiting.wait()
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
    else:
        assert "event: message" in await anext(stream)
        await stream.aclose()
    assert order == ["engine_closed", "lock_released", "request_released"]


@pytest.mark.asyncio
async def test_cancelled_flex_stream_closes_backend_before_partial_snapshot():
    order = []
    agent = object.__new__(FlexAgent)

    async def backend_stream(*_args, **_kwargs):
        try:
            yield ("values", {"partial": True})
        finally:
            await asyncio.sleep(0)
            order.append("backend_closed")

    async def read_final(**_kwargs):
        pytest.fail("Cancelled turns cannot be read as completed graph results")

    async def finalize(_initial, interrupted, **kwargs):
        assert interrupted is True
        assert kwargs["final_state"] == {}
        order.append("partial_snapshot")

    agent._read_langgraph_final_state_after_stream = read_final
    agent._finalize_context_after_stream = finalize
    agent.config = {}
    agent._create_call_runtime = lambda: SimpleNamespace()
    agent._inject_otel_config_from_yaml = lambda *_args: None
    agent._create_otel_recorder = lambda *_args: None
    agent._performance_run = lambda **_kwargs: nullcontext()
    agent._run_agent_pre_hooks = lambda state, _runtime: state
    agent._bind_workflow_runtime = lambda _runtime: None
    agent._flush_otel_recorder = lambda _recorder: None
    agent.workflow_backend = SimpleNamespace(astream=backend_stream)
    stream = agent.astream(initial_state={})
    await anext(stream)
    await stream.aclose()
    assert order == ["backend_closed", "partial_snapshot"]


@pytest.mark.asyncio
async def test_workflow_close_drains_compiled_graph():
    closed = asyncio.Event()

    async def compiled_stream(*_args, **_kwargs):
        try:
            yield {"partial": True}
        finally:
            await asyncio.sleep(0)
            closed.set()

    workflow = object.__new__(LangGraphWorkflow)
    workflow._get_compiled_graph = lambda **_kwargs: SimpleNamespace(astream=compiled_stream)
    workflow._resolve_runtime = lambda runtime: runtime
    workflow.merge_run_config = lambda *_args: {}
    workflow._build_run_context = lambda _runtime: None

    @contextmanager
    def active(_runtime):
        yield

    workflow._activate_runtime = active
    stream = workflow.astream({})
    await anext(stream)
    await stream.aclose()
    assert closed.is_set()
