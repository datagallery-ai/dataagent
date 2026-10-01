"""Configured models through native ToolRuntime.context, SDK and AG-UI."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from ag_ui_langgraph import LangGraphAgent
from conftest import ScriptedModel, call
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr
from test_api import client_for, query, sse, terminal
from test_traces import attributes, read_trace, spans

from dataagent import LaunchOptions, ModelContext, build_agent, create_agent, prepare_runtime
from dataagent.agent import load_extensions
from dataagent.models import build_model


@pytest.fixture
def tool_runtime(runtime):
    settings = runtime.settings.model_copy(update={
        "models": {
            **runtime.settings.models,
            "fast": runtime.settings.models["primary"].model_copy(update={
                "name": "fast-model", "api_key": SecretStr("secondary-private-value"), "max_retries": 0,
            }),
        },
        "plugins": runtime.settings.plugins.model_copy(update={
            "enabled": ("common", "llm-utils"),
            "paths": (Path(__file__).resolve().parents[1] / "examples/plugins/llm-utils",),
        }),
    })
    return replace(runtime, settings=settings)


def test_native_model_configuration_and_unknown_alias(tool_runtime):
    with patch("dataagent.models.ChatOpenAI") as create:
        settings = tool_runtime.settings
        build_model(tool_runtime, "fast")
        assert create.call_args.kwargs == {
            "model": "fast-model", "base_url": settings.models["fast"].base_url,
            "api_key": settings.models["fast"].api_key, "use_responses_api": False,
            "max_retries": 0, "timeout": settings.dataagent.limits.timeout_seconds,
        }
        with pytest.raises(ValueError, match="Unknown model alias"):
            build_model(tool_runtime, "missing")
        assert create.call_count == 1


def test_model_cache_default_selection_and_scope(tool_runtime):
    settings = tool_runtime.settings.model_copy(update={
        "dataagent": tool_runtime.settings.dataagent.model_copy(update={
            "model": tool_runtime.settings.dataagent.model.model_copy(update={"default": "fast"}),
        }),
    })
    tool_runtime = replace(tool_runtime, settings=settings)
    default = ScriptedModel(responses=[])
    other = ScriptedModel(responses=[])
    context = ModelContext(tool_runtime, model=default)
    with patch("dataagent.models.build_model", return_value=other) as create:
        assert context.get_model() is context.get_model("fast") is default
        assert context.get_model("primary") is context.get_model("primary") is other
        create.assert_called_once_with(tool_runtime, "primary")
        assert ModelContext(tool_runtime).get_model() is other
        assert create.call_count == 2  # A new configuration scope has its own cache.
    assert "secondary-private-value" not in repr(context)
    assert context.model_dump() == {}
    assert context.model_json_schema()["properties"] == {}


def test_concurrent_calls_construct_one_client(tool_runtime):
    context = ModelContext(tool_runtime)
    model = ScriptedModel(responses=[])
    with patch("dataagent.models.build_model", return_value=model) as create:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: context.get_model("fast"), range(32)))
        assert all(item is model for item in results)
        create.assert_called_once_with(tool_runtime, "fast")
    for name in ("missing", ""):
        with pytest.raises(ValueError, match="Unknown model alias"):
            context.get_model(name)


def test_model_access_is_not_an_llm_argument(tool_runtime):
    tool = next(tool for tool in load_extensions(tool_runtime)["tools"]
                if tool.name == "llm-utils__summarize_text")
    assert set(tool.tool_call_schema.model_json_schema()["properties"]) == {"text"}


def test_agui_inspects_compiled_graph_without_unbinding_execution(tool_runtime):
    graph = create_agent(tool_runtime, model=ScriptedModel(responses=[]))
    adapter = graph.as_agui(name="test")
    native = LangGraphAgent(name="test", graph=graph.graph)
    config = {"configurable": {"thread_id": "schema-test"}}
    keys = adapter.get_schema_keys(config)
    assert keys == native.get_schema_keys(config)
    assert "messages" in keys["output"]
    assert keys["context"] == []
    assert adapter.graph is graph.graph
    assert adapter.get_stream_kwargs({}, context={"model": "client-value"})["context"] is graph.context
    assert graph.as_agui() is not adapter


def test_secondary_environment_is_lazy_and_uses_startup_snapshot(runtime, monkeypatch):
    home = runtime.paths.home
    path = home / "config.json"
    settings = json.loads(path.read_text())
    settings["models"] = {"primary": {
        "name": "default", "base_url": "https://example.invalid/v1", "api_key": "primary-key",
    }, "fast": {
        "name": "$env{TOOL_MODEL}", "base_url": "$env{TOOL_URL}", "api_key": "$env{TOOL_KEY}",
    }, "unused": {
        "name": "unused", "base_url": "https://example.invalid", "api_key": "$env{MISSING_TOOL_KEY}",
    }}
    path.write_text(json.dumps(settings))
    (home / ".env").write_text(
        "TOOL_MODEL=fast-model\nTOOL_URL=https://example.invalid/v1\nTOOL_KEY=dotenv-secret\n"
    )
    monkeypatch.setenv("TOOL_KEY", "process-secret")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-capture")
    monkeypatch.delenv("MISSING_TOOL_KEY", raising=False)
    prepared = prepare_runtime(LaunchOptions(cwd=home))
    assert prepared.model_environment["TOOL_KEY"] == "process-secret"
    assert "UNRELATED_SECRET" not in prepared.model_environment
    assert "process-secret" not in repr(prepared)
    assert "process-secret" in prepared.redaction_secrets
    assert "fast-model" not in prepared.redaction_secrets
    assert "https://example.invalid/v1" not in prepared.redaction_secrets
    with pytest.raises(TypeError):
        prepared.model_environment["TOOL_KEY"] = "changed"
    # Neither later file changes nor process environment changes affect this runtime.
    monkeypatch.setenv("TOOL_KEY", "changed-secret")
    (home / ".env").write_text("TOOL_KEY=changed-file\n")
    context = ModelContext(prepared)
    with patch("dataagent.models.ChatOpenAI") as create:
        context.get_model("fast")
        assert create.call_args.kwargs["api_key"].get_secret_value() == "process-secret"
        assert create.call_args.kwargs["model"] == "fast-model"
        context.get_model("fast")
        assert create.call_count == 1
        with pytest.raises(ValueError, match="MISSING_TOOL_KEY"):
            context.get_model("unused")
        assert create.call_count == 1


async def test_raw_graph_remains_native_and_accepts_explicit_context(tool_runtime):
    from langgraph.graph.state import CompiledStateGraph

    context = ModelContext(tool_runtime, model=ScriptedModel(responses=agent_responses(False)))
    graph = build_agent(tool_runtime, context=context)
    assert isinstance(graph, CompiledStateGraph)
    assert "messages" in graph.get_output_jsonschema()["properties"]
    with patch("dataagent.models.build_model", return_value=ScriptedModel(responses=[AIMessage(content="helper")])):
        result = await graph.ainvoke({"messages": [("user", "summarize")]}, context=context)
    assert result["messages"][-1].content == "done"


async def test_session_contexts_do_not_share_model_clients(tool_runtime):
    helpers = [ScriptedModel(responses=[AIMessage(content=f"helper-{i}")]) for i in range(2)]
    with patch("dataagent.models.build_model", side_effect=helpers) as create:
        async with client_for(tool_runtime, ScriptedModel(responses=[
            *agent_responses(False), *agent_responses(False),
        ])) as (client, app):
            # Separate sessions must own separate contexts even with identical config.
            for thread in ("first", "second"):
                body = {**query(), "threadId": thread}
                assert terminal(sse(await client.post("/dataagent/stream", json=body))) == ["RUN_FINISHED"]
            first, second = app.state.agents.cache.values()
            assert first.context is not second.context
            assert first.context.get_model("fast") is helpers[0]
            assert second.context.get_model("fast") is helpers[1]
            assert create.call_count == 2


async def test_concurrent_sdk_agents_keep_their_own_context(tool_runtime):
    entered = [asyncio.Event(), asyncio.Event()]

    class InterleavedModel(ScriptedModel):
        index: int

        async def _agenerate(self, *args, **kwargs):
            entered[self.index].set()
            await asyncio.wait_for(entered[1 - self.index].wait(), 3)
            return await super()._agenerate(*args, **kwargs)

    agents = [create_agent(tool_runtime, model=ScriptedModel(responses=agent_responses(False))) for _ in range(2)]
    helpers = [InterleavedModel(index=i, responses=[AIMessage(content=f"helper-{i}")]) for i in range(2)]
    with patch("dataagent.models.build_model", side_effect=helpers):
        for agent in agents:
            agent.context.get_model("fast")
        results = await asyncio.gather(*[
            agent.ainvoke({"messages": [("user", "summarize")]}) for agent in agents
        ])
    for i, result in enumerate(results):
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert tool_messages[-1].content == f"helper-{i}"


@pytest.mark.parametrize("stream", [False, True])
def test_native_sync_tool_uses_configured_default_model(tool_runtime, stream):
    @tool
    def summarize_sync(text: str, runtime: ToolRuntime[ModelContext]) -> str:
        """Summarize text with the default model."""
        return runtime.context.get_model().invoke(text, config=runtime.config).text

    model = ScriptedModel(responses=[
        call("summarize_sync", {"text": "input"}),
        AIMessage(content="internal summary"), AIMessage(content="final answer"),
    ])
    graph = create_agent(tool_runtime, model=model, mcp_tools=[summarize_sync])
    if stream:
        result = list(graph.stream({"messages": [("user", "summarize")]}, stream_mode="values"))[-1]
    else:
        result = graph.invoke({"messages": [("user", "summarize")]})
    assert result["messages"][-1].content == "final answer"
    assert next(m for m in result["messages"] if isinstance(m, ToolMessage)).content == "internal summary"


def test_model_is_native_and_supports_structured_output(tool_runtime):
    from langchain_openai import ChatOpenAI
    from pydantic import BaseModel

    class Summary(BaseModel):
        text: str

    model = ModelContext(tool_runtime).get_model("fast")
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "fast-model"
    assert model.with_structured_output(Summary) is not None


def agent_responses(child):
    responses = [call("llm-utils__summarize_text", {"text": "three rows"}), AIMessage(content="done")]
    if child:
        responses = [call("task", {"subagent_type": "general-purpose", "description": "Summarize text"}),
                     *responses, AIMessage(content="parent done")]
    return responses


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("mode", ["invoke", "stream", "events"])
async def test_sdk_owned_context_reaches_root_and_child_tools(tool_runtime, child, mode):
    model = ScriptedModel(responses=agent_responses(child))
    helper = ScriptedModel(responses=[AIMessage(content="Only helper output")])
    graph = create_agent(tool_runtime, model=model)
    with patch("dataagent.models.build_model", return_value=helper) as create:
        if mode == "stream":
            async for _ in graph.astream({"messages": [("user", "summarize")]}):
                pass
        elif mode == "events":
            async for _ in graph.astream_events({"messages": [("user", "summarize")]}, version="v2"):
                pass
        else:
            await graph.ainvoke({"messages": [("user", "summarize")]})
        create.assert_called_once_with(tool_runtime, "fast")
    assert helper.cursor == 1
    assert helper.requests[0][-1].content == "three rows"
    tool_messages = [message for request in model.requests for message in request
                     if isinstance(message, ToolMessage) and message.name == "llm-utils__summarize_text"]
    assert tool_messages and all(message.content == "Only helper output" for message in tool_messages)


@pytest.mark.parametrize("child", [False, True])
async def test_http_preserves_owned_context_and_traces_nested_model(tool_runtime, child):
    model = ScriptedModel(responses=[*agent_responses(child), *agent_responses(child)])
    helper = ScriptedModel(responses=[AIMessage(content="Only helper output") for _ in range(2)])
    with patch("dataagent.models.build_model", return_value=helper) as create:
        async with client_for(tool_runtime, model) as (client, app):
            for _ in range(2):
                body = query()
                response = await client.post("/dataagent/stream", json=body)
                assert terminal(sse(response)) == ["RUN_FINISHED"]
            assert helper.cursor == 2
            create.assert_called_once_with(tool_runtime, "fast")
            assert not hasattr(app.state.agents, "model_context")
            graph = next(iter(app.state.agents.cache.values()))
            assert graph.context.get_model("fast") is helper
            state = await app.state.checkpointer.aget_tuple({"configurable": {"thread_id": body["threadId"]}})
            persisted = repr((state.checkpoint, state.metadata, state.config))
            assert "ModelContext" not in persisted and "secondary-private-value" not in persisted
    records = spans(read_trace(tool_runtime, body))
    by_id = {item["spanId"]: item for item in records}
    nested_models = [item for item in records if attributes(item).get("langsmith.span.kind") == "llm"
                     and item.get("parentSpanId") in by_id
                     and by_id[item["parentSpanId"]]["name"] == "llm-utils__summarize_text"]
    assert nested_models  # runtime.config preserved the real tool -> model parent link.


async def test_http_unknown_alias_is_real_error_without_fallback(tool_runtime):
    settings = tool_runtime.settings.model_copy(update={"models": {"primary": tool_runtime.settings.models["primary"]}})
    tool_runtime = replace(tool_runtime, settings=settings)
    model = ScriptedModel(responses=agent_responses(False))
    async with client_for(tool_runtime, model) as (client, _):
        events = sse(await client.post("/dataagent/stream", json=query()))
    assert terminal(events) == ["RUN_ERROR"]
    assert "Unknown model alias: 'fast'" in events[-1]["message"]
    assert model.cursor == 1


async def test_secondary_model_errors_are_redacted(tool_runtime):
    class FailingModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            raise RuntimeError("provider failure secondary-private-value")

    model = ScriptedModel(responses=agent_responses(False))
    with patch("dataagent.models.build_model", return_value=FailingModel(responses=[])):
        async with client_for(tool_runtime, model) as (client, _):
            body = query()
            response = await client.post("/dataagent/stream", json=body)
    assert terminal(sse(response)) == ["RUN_ERROR"]
    assert "secondary-private-value" not in response.text
    assert "secondary-private-value" not in json.dumps(read_trace(tool_runtime, body))


async def test_http_deadline_cancels_internal_model(tool_runtime):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class SlowModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    tool_runtime = replace(tool_runtime, settings=tool_runtime.settings.model_copy(update={
        "dataagent": tool_runtime.settings.dataagent.model_copy(update={
            "limits": tool_runtime.settings.dataagent.limits.model_copy(update={"timeout_seconds": 0.5}),
        }),
    }))
    with patch("dataagent.models.build_model", return_value=SlowModel(responses=[])):
        async with client_for(tool_runtime, ScriptedModel(responses=agent_responses(False))) as (client, _):
            events = sse(await client.post("/dataagent/stream", json=query()))
    assert entered.is_set() and cancelled.is_set()
    assert terminal(events) == ["RUN_ERROR"]
    assert events[-1]["code"] == "TimeoutError"


async def test_sdk_checkpoint_resume_rebuilds_owned_context_not_serialized_clients(tool_runtime):
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "resume-models"}}
    helper = ScriptedModel(responses=[AIMessage(content="first helper"), AIMessage(content="second helper")])
    with patch("dataagent.models.build_model", return_value=helper):
        for _ in range(2):
            graph = create_agent(tool_runtime, model=ScriptedModel(responses=agent_responses(False)), checkpointer=saver)
            await graph.ainvoke({"messages": [("user", "summarize")]}, config)
    state = await graph.graph.aget_state(config)
    assert len([m for m in state.values["messages"] if m.type == "human"]) == 2
    assert "ModelContext" not in repr(await saver.aget_tuple(config))


async def test_internal_model_cancellation_propagates(tool_runtime):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class SlowModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    graph = create_agent(tool_runtime, model=ScriptedModel(responses=agent_responses(False)))
    with patch("dataagent.models.build_model", return_value=SlowModel(responses=[])):
        task = asyncio.create_task(graph.ainvoke({"messages": [("user", "summarize")]}))
        try:
            await asyncio.wait_for(entered.wait(), 3)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert cancelled.is_set()
