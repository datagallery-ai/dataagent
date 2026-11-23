"""One SDK instance, cached session graphs, native execution and trace isolation."""

import asyncio
import json
from contextlib import closing
from unittest.mock import patch

import pytest
from conftest import RecordingCallback, ScriptedModel, call
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import get_runtime
from test_output_directory import configured
from test_traces import assert_collector_empty, attributes, root, spans, validate_file

from dataagent import create_agent
from dataagent.assembly.context import AgentContext
from dataagent.assembly.graph import create_deep_agent, load_extensions
from dataagent.extensions.tracing import LocalLangChainTracer


@pytest.mark.parametrize("path", ["reports", "./reports", "../reports", "~other/reports"])
def test_reject_relative_output_before_creating_session(runtime, path):
    with pytest.raises(ValueError, match="absolute path or start with ~/"):
        runtime.paths.open_session("rejected", create=True, output_dir=path)
    assert not runtime.paths.for_session("rejected").root.exists()
    with pytest.raises(ValueError, match="absolute path or start with ~/"):
        configured(runtime, path)


async def test_session_graphs_concurrent_files_shell_artifacts_trace_and_callbacks(
    runtime, tmp_path
):
    entered = set()
    ready = asyncio.Event()
    seen_contexts = []

    @tool
    def evidence(runtime: ToolRuntime[AgentContext]) -> str:
        """Produce enough evidence to trigger native tool result offloading."""
        seen_contexts.append(runtime.context)
        return runtime.context.session.root.name * 25000

    class TaskModel(ScriptedModel):
        def _generate(self, messages, *args, **kwargs):
            context = get_runtime().context
            task = context.session.root.name
            output = context.session.outputs
            self.requests.append(messages)
            assert str(output) in str(messages[0].content)
            tools = [m for m in messages if isinstance(m, ToolMessage)]
            steps = [
                call("execute", {"command": f"printf {task} > report.txt"}),
                call("read_file", {"file_path": str(output / "report.txt")}),
                call("glob", {"pattern": "*.txt", "path": str(output)}),
                call("evidence", {}, id="same-call-id"),
                AIMessage(content=task),
            ]
            return ChatResult(generations=[ChatGeneration(message=steps[len(tools)])])

        async def _agenerate(self, messages, *args, **kwargs):
            entered.add(get_runtime().context.session.root.name)
            if len(entered) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 5)
            return self._generate(messages)

    model = TaskModel(responses=[])
    observer = RecordingCallback()
    with patch("dataagent.assembly.graph.create_deep_agent", wraps=create_deep_agent) as build:
        async with create_agent(runtime, model=model, mcp_tools=[evidence]) as agent:
            results = await asyncio.gather(
                *(
                    agent.ainvoke(
                        {"messages": [("user", task)]},
                        {"configurable": {"thread_id": task}, "callbacks": [observer]},
                        output_dir=tmp_path / task,
                    )
                    for task in ("task-a", "task-b")
                )
            )
            from langgraph.graph.state import CompiledStateGraph

            first = agent.graph_for("task-a")
            assert isinstance(first, CompiledStateGraph)
            assert agent.graph_for("task-a") is first
            assert agent.graph_for("task-b") is not first
            assert build.call_count == 2
            assert not agent._active
            assert len(seen_contexts) == 2 and seen_contexts[0] is not seen_contexts[1]
            assert all(c.get_model() is model for c in seen_contexts)
    for task, result in zip(("task-a", "task-b"), results, strict=True):
        output = tmp_path / task
        assert (output / "report.txt").read_text() == task
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert all(m.status == "success" for m in tool_messages)
        assert task in tool_messages[1].content
        assert str(output / "report.txt") in tool_messages[2].content
        files = list((output / "artifacts/large_tool_results").rglob("*"))
        assert any(p.is_file() and task in p.read_text() for p in files)
        assert str(output / "artifacts") in tool_messages[-1].content
        session = runtime.paths.for_session(task)
        (trace,) = session.traces.glob("*.json")
        data = validate_file(trace)
        assert task in json.dumps(data)
        assert ("task-b" if task == "task-a" else "task-a") not in trace.read_text()
        assert session.traces.is_relative_to(runtime.paths.home)
    assert {e[2]["thread_id"] for e in observer.events} == {"task-a", "task-b"}
    assert not runtime.paths.for_session("sdk").root.exists()


async def test_session_graphs_subagent_paths_and_checkpoint_history(runtime, tmp_path):
    responses = []
    for task in ("one", "two"):
        responses.extend(
            [
                call("task", {"subagent_type": "general-purpose", "description": task}),
                call("execute", {"command": f"printf {task} > child.txt"}),
                AIMessage(content="child done"),
                AIMessage(content="root done"),
            ]
        )
    model = ScriptedModel(responses=[*responses, AIMessage(content="resumed")])
    async with create_agent(runtime, model=model, checkpointer=InMemorySaver()) as agent:
        for task in ("one", "two"):
            await agent.ainvoke(
                {"messages": [("user", task)]},
                {"configurable": {"thread_id": task}},
                output_dir=tmp_path / task,
            )
            assert (tmp_path / task / "child.txt").read_text() == task
        assert str(tmp_path / "one") in str(model.requests[1][0].content)
        assert str(tmp_path / "two") in str(model.requests[5][0].content)
        assert str(tmp_path / "one") not in str(model.requests[5][0].content)
        first = agent.graph_for("one")
        callbacks = list(first.config["callbacks"])
        await agent.ainvoke(
            {"messages": [("user", "continue")]}, {"configurable": {"thread_id": "one"}}
        )
        assert agent.graph_for("one") is first
        assert first.config["callbacks"] == callbacks
        assert_collector_empty(first)
        assert len(list(runtime.paths.for_session("one").traces.glob("*.json"))) == 2
        assert [m.content for m in model.requests[-1] if isinstance(m, HumanMessage)] == [
            "one",
            "continue",
        ]
        count = model.cursor
        with pytest.raises(ValueError, match="already bound"):
            await agent.ainvoke(
                {}, {"configurable": {"thread_id": "one"}}, output_dir=tmp_path / "moved"
            )
        assert model.cursor == count and not (tmp_path / "moved").exists()


@pytest.mark.parametrize("method", ["invoke", "stream", "ainvoke", "astream", "astream_events"])
async def test_all_sdk_entrypoints_use_invocation_paths(runtime, tmp_path, method):
    model = ScriptedModel(
        responses=[call("execute", {"command": "printf ok > marker"}), AIMessage(content="done")]
    )
    async with create_agent(runtime, model=model) as agent:
        args = ({"messages": [("user", "save")]}, {"configurable": {"thread_id": method}})
        result = getattr(agent, method)(*args, output_dir=tmp_path / method)
        if method == "ainvoke":
            await result
        elif method in ("astream", "astream_events"):
            async for _ in result:
                pass
        elif method == "stream":
            list(result)
        assert (tmp_path / method / "marker").read_text() == "ok"
        assert not agent._active
        assert list(runtime.paths.for_session(method).traces.glob("*.json"))


@pytest.mark.parametrize("config", [None, {}, {"configurable": None}, {"configurable": {}}])
def test_empty_configurable_uses_default_session(runtime, config):
    model = ScriptedModel(responses=[AIMessage(content="done")])
    with create_agent(runtime, model=model, checkpointer=InMemorySaver()) as agent:
        result = agent.invoke({"messages": [("user", "hello")]}, config)
        assert result["messages"][-1].content == "done"
        assert set(agent._graphs) == {"sdk"}
        assert not agent._active
        state = agent.graph.get_state({"configurable": {"thread_id": "sdk"}})
        assert state.values["messages"][-1].content == "done"


async def test_same_session_busy_and_cancellation_release(runtime, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            entered.set()
            await release.wait()
            return await super()._agenerate(*args, **kwargs)

    async with create_agent(
        runtime, model=WaitingModel(responses=[AIMessage(content="done")])
    ) as agent:
        inputs, config = {"messages": [("user", "wait")]}, {"configurable": {"thread_id": "same"}}
        task = asyncio.create_task(agent.ainvoke(inputs, config, output_dir=tmp_path / "out"))
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(ValueError, match="already running"):
            await agent.ainvoke(inputs, config)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not agent._active
        release.set()
        assert (await agent.ainvoke(inputs, config))["messages"][-1].content == "done"


async def test_shared_summary_history_uses_current_artifacts(runtime, tmp_path):
    from dataagent.core.middleware import SummarizationMiddleware

    summary_model = ScriptedModel(responses=[AIMessage(content="summary")] * 2)

    def summary(_, backend):
        return SummarizationMiddleware(
            model=summary_model,
            backend=backend,
            trigger=("messages", 4),
            keep=("messages", 2),
        )

    with patch("dataagent.core.graph.create_summarization_middleware", side_effect=summary):
        async with create_agent(
            runtime, model=ScriptedModel(responses=[AIMessage(content="done")] * 2)
        ) as agent:
            for task in ("apples", "oranges"):
                await agent.ainvoke(
                    {
                        "messages": [
                            HumanMessage(content=task),
                            AIMessage(content="noted"),
                            HumanMessage(content=task),
                            AIMessage(content="noted"),
                            HumanMessage(content="continue"),
                        ]
                    },
                    {"configurable": {"thread_id": task}},
                    output_dir=tmp_path / task,
                )
                files = [
                    p
                    for p in (tmp_path / task / "artifacts/conversation_history").rglob("*")
                    if p.is_file()
                ]
                assert files and any(task in p.read_text() for p in files)
                other = "oranges" if task == "apples" else "apples"
                assert all(other not in p.read_text() for p in files)


@pytest.mark.parametrize("entry", [None, "nl2sql"])
async def test_shared_nl2sql_and_run_hooks_use_current_task(runtime, tmp_path, entry):
    from test_nl2sql_plugin import _PLUGIN_RESPONSES, _plugin_params, _sqlite_fixture

    from dataagent import LaunchOptions, prepare_runtime

    db, schema = _sqlite_fixture(tmp_path)
    params = _plugin_params(db, schema)
    params.update(model="sql", run_hooks={"before_run": [{"entrypoint": "hook.py:handle"}]})
    (tmp_path / "hook.py").write_text(
        "def handle(state, runtime, *, params):\n"
        "    path = runtime.context.session.outputs\n"
        '    (path / "hook.txt").write_text(runtime.context.session.root.name)\n'
    )
    config = tmp_path / "shared.yaml"
    config.write_text(
        json.dumps(
            {
                "models": {
                    name: {"name": "test", "base_url": "https://example.invalid", "api_key": "test"}
                    for name in ("primary", "sql")
                },
                "dataagent": {"entry": entry},
                "plugins": {"enabled": ["nl2sql"], "config": {"nl2sql": params}},
            }
        )
    )
    runtime = prepare_runtime(LaunchOptions(config=config))
    entered, counters = set(), {}
    ready = asyncio.Event()

    class QueryModel(ScriptedModel):
        async def _agenerate(self, messages, *args, **kwargs):
            thread = get_runtime().context.session.root.name
            entered.add(thread)
            if len(entered) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 5)
            index = counters.get(thread, 0)
            counters[thread] = index + 1
            self.requests.append(messages)
            return ChatResult(generations=[ChatGeneration(message=_PLUGIN_RESPONSES[index])])

    class ParentModel(ScriptedModel):
        async def _agenerate(self, messages, *args, **kwargs):
            reply = (
                AIMessage(content="done")
                if any(isinstance(m, ToolMessage) for m in messages)
                else call("task", {"subagent_type": "nl2sql", "description": "每个部门的员工人数"})
            )
            return ChatResult(generations=[ChatGeneration(message=reply)])

    with patch("dataagent.assembly.context.build_model", return_value=QueryModel(responses=[])):
        with patch(
            "dataagent.assembly.graph.load_extensions", wraps=load_extensions
        ) as compile_plugins:
            async with create_agent(runtime, model=ParentModel(responses=[])) as agent:
                results = await asyncio.gather(
                    *(
                        agent.ainvoke(
                            {"messages": [("user", "每个部门的员工人数")]},
                            {"configurable": {"thread_id": thread}},
                            output_dir=tmp_path / thread,
                        )
                        for thread in ("query-a", "query-b")
                    )
                )
                assert compile_plugins.call_count == 2
                if entry:
                    for thread in ("query-a", "query-b"):
                        (tracer,) = (
                            callback
                            for callback in agent.graph_for(thread).config["callbacks"]
                            if isinstance(callback, LocalLangChainTracer)
                        )
                        assert tracer.agent_names == (entry,)
    for thread, result in zip(("query-a", "query-b"), results, strict=True):
        message = (
            result["messages"][-1]
            if entry
            else next(m for m in result["messages"] if isinstance(m, ToolMessage))
        )
        payload = json.loads(message.content)
        assert payload["error"] is None
        assert payload["result_file"].startswith(str(tmp_path / thread / "nl2sql"))
        assert (tmp_path / thread / "hook.txt").read_text() == thread
        assert counters[thread] == 3
        (trace,) = runtime.paths.for_session(thread).traces.glob("*.json")
        data = validate_file(trace)
        agent_span = root(data) if entry else next(s for s in spans(data) if s["name"] == "nl2sql")
        assert "每个部门的员工人数" in attributes(agent_span)["gen_ai.prompt"]
        assert "result_file" in attributes(agent_span)["gen_ai.completion"]


async def test_agui_selects_session_graph_on_one_sdk_instance(runtime, tmp_path):
    from ag_ui.core import RunAgentInput
    from test_api import query

    runtime = configured(runtime, str(tmp_path / "reports"))
    # A stale default output must not prevent unrelated AG-UI threads from running.
    default_session = runtime.paths.for_session("sdk")
    default_session.outputs.mkdir(parents=True)
    marker = default_session.outputs / "unowned.txt"
    marker.write_text("preserve")
    model = ScriptedModel(
        responses=[
            call("execute", {"command": "printf first > marker"}),
            AIMessage(content="first"),
            call("execute", {"command": "printf second > marker"}),
            AIMessage(content="second"),
        ]
    )
    async with create_agent(runtime, model=model, checkpointer=InMemorySaver()) as agent:
        for thread in ("first", "second"):
            adapter = agent.as_agui(emit_raw_events=False).clone()
            assert adapter.graph is None
            assert not default_session.root.exists()
            events = [event async for event in adapter.run(RunAgentInput(**query(thread=thread)))]
            assert events[-1].type == "RUN_FINISHED"
            assert all(event.type != "RAW" for event in events)
            assert adapter.graph is agent.graph_for(thread)
            assert (tmp_path / "reports/default" / thread / "marker").read_text() == thread
            (trace,) = runtime.paths.for_session(thread).traces.glob("*.json")
            validate_file(trace)
        assert set(agent._graphs) == {"first", "second"}
        assert not default_session.root.exists()
        assert marker.read_text() == "preserve"


def test_graph_lookup_does_not_create_or_bind_a_session(runtime, tmp_path):
    model = ScriptedModel(responses=[AIMessage(content="done")])
    with create_agent(runtime, model=model, checkpointer=InMemorySaver()) as agent:
        with pytest.raises(ValueError, match="does not exist"):
            agent.graph_for("later")
        assert not runtime.paths.for_session("later").root.exists()
        config = {"configurable": {"thread_id": "later"}}
        agent.invoke({"messages": [("user", "go")]}, config, output_dir=tmp_path / "later")
        assert runtime.paths.for_session("later").outputs == tmp_path / "later"
        agent.as_agui()
        assert not runtime.paths.for_session("sdk").root.exists()


def test_cached_graph_lookup_does_not_wait_for_another_session_build(runtime):
    import threading

    from dataagent import client

    started, release = threading.Event(), threading.Event()
    real_build = client.build_graph

    def slow_build(*args, **kwargs):
        if kwargs["session"].root.name == "slow":
            started.set()
            release.wait(5)
        return real_build(*args, **kwargs)

    with create_agent(runtime, model=ScriptedModel(responses=[])) as agent:
        fast = agent.graph
        with patch.object(client, "build_graph", slow_build):
            builder = threading.Thread(target=agent._resolve, args=("slow",))
            builder.start()
            assert started.wait(5)
            try:
                assert agent.graph is fast
                assert agent._cached("slow") is None
            finally:
                release.set()
                builder.join(5)
        assert agent._cached("slow") is not None


def test_graph_cache_evicts_idle_sessions_only(runtime, monkeypatch):
    from dataagent import client

    monkeypatch.setattr(client, "_MAX_CACHED_GRAPHS", 2)
    with create_agent(runtime, model=ScriptedModel(responses=[])) as agent:
        agent._resolve("running")
        with agent._claim("running"):
            for thread in ("a", "b", "c"):
                agent._resolve(thread)
        assert list(agent._graphs) == ["running", "c"]
        assert agent.graph_for("a") is not None
        assert list(agent._graphs) == ["c", "a"]


def test_explicitly_closed_stream_releases_its_session_without_garbage_collection(runtime):
    model = ScriptedModel(responses=[AIMessage(content="first"), AIMessage(content="second")])
    with create_agent(runtime, model=model, checkpointer=InMemorySaver()) as agent:
        stream = agent.stream({"messages": [("user", "go")]})
        with closing(stream):
            next(stream)
            assert agent._active == {"sdk"}
            with pytest.raises(ValueError, match="already running"):
                agent.invoke({"messages": [("user", "overlap")]})
        assert not agent._active
        agent.invoke({"messages": [("user", "again")]})


def test_stream_checks_paths_eagerly_and_does_not_claim_before_iteration(runtime, tmp_path):
    with create_agent(runtime, model=ScriptedModel(responses=[])) as agent:
        with pytest.raises(ValueError, match="absolute path"):
            agent.stream({}, output_dir="relative-output")
        assert not runtime.paths.for_session("sdk").root.exists()
        config = {"configurable": {"thread_id": "stream"}}
        stream = agent.stream({}, config, output_dir=tmp_path / "first")
        assert not agent._active
        with pytest.raises(ValueError, match="already bound"):
            agent.stream({}, config, output_dir=tmp_path / "different")
        stream.close()  # Closing an unstarted stream must not leave a claim either.
        assert not agent._active


def test_stream_failure_releases_session(runtime):
    with create_agent(runtime, model=ScriptedModel(responses=[])) as agent:
        stream = agent.stream({"messages": [("user", "fail")]})
        with pytest.raises(RuntimeError, match="ran out of responses"):
            list(stream)
        assert not agent._active


@pytest.mark.parametrize("compiled", [False, True])
def test_configuration_preflight_never_imports_plugin_python(runtime, tmp_path, compiled):
    import sys

    from test_agent import _runtime_with_entry

    from dataagent.extensions.loading import PythonLoader

    if compiled:
        runtime, _ = _runtime_with_entry(runtime, tmp_path, "direct")
    paths = list(sys.path)
    modules = {
        key: value for key, value in sys.modules.items() if key.startswith("_dataagent_plugin_")
    }
    with patch.object(
        PythonLoader, "load", side_effect=AssertionError("preflight imported Python")
    ):
        with create_agent(runtime, model=ScriptedModel(responses=[])) as agent:
            adapter = agent.as_agui()
            assert adapter.graph is None
            assert not agent._graphs
    assert sys.path == paths
    assert {
        key: value for key, value in sys.modules.items() if key.startswith("_dataagent_plugin_")
    } == modules
    assert not runtime.paths.sessions_db.exists()


def test_create_agent_rejects_configuration_before_any_session(runtime):
    from test_agent import _with_entry

    with pytest.raises(ValueError, match="Available compiled subagents"):
        create_agent(_with_entry(runtime, "missing"), model=ScriptedModel(responses=[]))
    assert not (runtime.paths.home / "runtime/users").exists()


def test_create_agent_rejects_unknown_keyword(runtime) -> None:
    with pytest.raises(TypeError, match="checkpointr"):
        create_agent(runtime, model=ScriptedModel(responses=[]), checkpointr=None)
