import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import RecordingCallback, ScriptedModel, call
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from dataagent.agent import build_graph
from dataagent.bootstrap import LaunchOptions, prepare_runtime
from dataagent.declarations import HookSpec
from dataagent.models import AgentContext


def test_build_returns_native_graph_without_callback_adapter(runtime):
    graph = build_graph(runtime, model=ScriptedModel(responses=[]))
    assert isinstance(graph, CompiledStateGraph)
    with pytest.raises(TypeError, match="callbacks"):
        build_graph(runtime, model=ScriptedModel(responses=[]), callbacks=[])


def test_independent_hooks_without_bindings_fail_explicitly(runtime):
    settings = runtime.settings.model_copy(update={
        "dataagent": runtime.settings.dataagent.model_copy(update={
            "hooks": (HookSpec(event="before_agent", entrypoint="missing.py:handle"),),
        }),
    })
    with pytest.raises(ValueError, match="Hook bindings do not match"):
        build_graph(replace(runtime, settings=settings), model=ScriptedModel(responses=[]))


async def test_builtin_audit_uses_six_hooks_without_logging_payloads(runtime, caplog):
    caplog.set_level("INFO", logger="dataagent.audit")
    model = ScriptedModel(responses=[
        call("common__summarize_numbers", {"numbers": [98765]}), AIMessage(content="private-answer"),
    ])
    await build_graph(runtime, model=model).ainvoke({"messages": [HumanMessage(content="private-question")]})
    records = [record.getMessage() for record in caplog.records if record.name == "dataagent.audit"]
    assert [record.split()[0] for record in records] == [
        "hook.before_agent", "hook.before_model", "hook.after_model", "hook.before_tool",
        "hook.after_tool", "hook.before_model", "hook.after_model", "hook.after_agent",
    ]
    assert all("agent=dataagent" in record for record in records)
    assert not any(value in "\n".join(records) for value in ("private-question", "private-answer", "98765", "test-secret"))


async def test_real_tool_and_sdk_hooks(runtime):
    model = ScriptedModel(responses=[
        call("common__summarize_numbers", {"numbers": [1, 2, 3]}), AIMessage(content="Mean: 2"),
    ])
    hooks = RecordingCallback()
    graph = build_graph(runtime, model=model).with_config(callbacks=[hooks])
    result = await graph.ainvoke({"messages": [HumanMessage(content="Compute 1, 2, 3")]})
    output = next(message for message in result["messages"] if isinstance(message, ToolMessage))
    assert json.loads(output.content) == {"count": 3, "sum": 6, "mean": 2, "min": 1, "max": 3}
    assert [event for _, event, _ in hooks.events] == [
        "on_chain_start", "on_tool_start", "on_tool_end", "on_chain_end",
    ]
    assert not hooks.active


async def test_task_skill_tool_chain_and_child_hooks(runtime, caplog):
    caplog.set_level("INFO", logger="dataagent.audit")
    skill = str(runtime.paths.home / "plugins/common/skills/number-summary/SKILL.md")
    model = ScriptedModel(responses=[
        call("task", {"subagent_type": "general-purpose", "description": "Compute 1,2,3"}),
        call("read_file", {"file_path": skill}),
        call("common__summarize_numbers", {"numbers": [1, 2, 3]}),
        AIMessage(content="Count 3, sum 6, mean 2, min 1, max 3"),
        AIMessage(content="Mean: 2"),
    ])
    hooks = RecordingCallback()
    result = await build_graph(runtime, model=model).with_config(callbacks=[hooks]).ainvoke({
        "messages": [HumanMessage(content="Delegate the statistics")],
    })
    assert result["messages"][-1].content == "Mean: 2"
    assert [(name, event) for name, event, _ in hooks.events if event == "on_tool_end"] == [
        ("read_file", "on_tool_end"), ("common__summarize_numbers", "on_tool_end"),
        ("task", "on_tool_end"),
    ]
    scopes = {agent: details for agent, event, details in hooks.events if event == "on_chain_start"}
    root, child = scopes["dataagent"], scopes["general-purpose"]
    assert root["parent_run_id"] is None
    assert child["parent_run_id"] is not None  # Native parent can be an internal task run.
    assert child["run_id"] != root["run_id"]
    assert not hooks.active
    for event in ("before_agent", "after_agent", "before_model", "after_model", "before_tool", "after_tool"):
        assert f"hook.{event} agent=general-purpose" in caplog.text


async def test_general_purpose_can_read_tabular_skill(runtime):
    skill = str(runtime.paths.home / "plugins/common/skills/tabular-inspection/SKILL.md")
    model = ScriptedModel(responses=[
        call("task", {"subagent_type": "general-purpose", "description": "Read the tabular inspection workflow"}),
        call("read_file", {"file_path": skill}),
        AIMessage(content="Use csv to inspect rows without changing inputs."),
        AIMessage(content="Workflow ready."),
    ])
    await build_graph(runtime, model=model).ainvoke({"messages": [HumanMessage(content="Inspect CSV workflow")]})
    assert any(isinstance(message, ToolMessage) and "# Tabular inspection" in str(message.content)
               for request in model.requests for message in request)
    assert "execute" in model.tool_schemas[1]


@pytest.mark.parametrize("args", [{"numbers": []}, {"numbers": "wrong-type"}])
async def test_error_tool_message_is_observed_not_false_success(runtime, args):
    hooks = RecordingCallback()
    model = ScriptedModel(responses=[
        call("common__summarize_numbers", args), AIMessage(content="Please provide numbers"),
    ])
    result = await build_graph(runtime, model=model).with_config(callbacks=[hooks]).ainvoke({
        "messages": [HumanMessage(content="test")],
    })
    events = [event for _, event, _ in hooks.events]
    errors = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert len(errors) == 1 and errors[0].status == "error"
    assert events[-1] == "on_chain_end"  # A correctable tool error need not fail the entire Agent.


async def test_model_error_propagates_without_retry(runtime):
    hooks = RecordingCallback()
    model = ScriptedModel(responses=[])
    with pytest.raises(RuntimeError, match="ran out"):
        await build_graph(runtime, model=model).with_config(callbacks=[hooks]).ainvoke({
            "messages": [HumanMessage(content="test")],
        })
    assert len(model.requests) == 1
    assert hooks.events[-1][1] == "on_chain_error"
    assert not hooks.active


@pytest.mark.parametrize("child", [False, True])
async def test_default_limits_allow_128_model_and_tool_calls(runtime, child):
    limits = runtime.settings.dataagent.limits
    assert limits.model_calls_per_agent == limits.tool_calls_per_agent == 128
    # Distinct work exercises the call budget without triggering loop protection.
    calls = [call("common__summarize_numbers", {"numbers": [index]}, id=f"stats-{index}")
             for index in range(128)]
    # Two tools in the first model response leave room for the final model answer.
    responses = [AIMessage(content="", tool_calls=calls[0].tool_calls + calls[1].tool_calls),
                 *calls[2:], AIMessage(content="done")]
    if child:
        responses = [call("task", {"subagent_type": "general-purpose", "description": "Compute statistics"}),
                     *responses, AIMessage(content="done")]
    model = ScriptedModel(responses=responses)
    graph = build_graph(runtime, model=model)
    result = await graph.ainvoke({"messages": [HumanMessage(content="Compute statistics")]})
    assert result["messages"][-1].content == "done"
    assert len(model.requests) == 128 + (2 if child else 0)
    tool_results = [message for message in model.requests[-2 if child else -1]
                    if isinstance(message, ToolMessage) and message.name == "common__summarize_numbers"]
    assert len(tool_results) == 128
    assert all(message.status == "success" for message in tool_results)


async def test_run_limit_resets_across_turns_and_overflow_is_error(runtime):
    runtime = replace(runtime, settings=runtime.settings.model_copy(update={"dataagent": runtime.settings.dataagent.model_copy(update={
        "limits": runtime.settings.dataagent.limits.model_copy(update={"model_calls_per_agent": 1}),
    })}))
    model = ScriptedModel(responses=[AIMessage(content="one"), AIMessage(content="two")])
    graph = build_graph(runtime, model=model, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "persist"}}
    for text in ["first", "second"]:
        result = await graph.ainvoke({"messages": [HumanMessage(content=text)]}, config)
    assert len(result["messages"]) == 4
    model = ScriptedModel(responses=[call("common__summarize_numbers", {"numbers": [1]})])
    with pytest.raises(Exception, match="Model call limits exceeded"):
        await build_graph(runtime, model=model).ainvoke({"messages": [HumanMessage(content="test")]})


async def test_workspace_readonly_is_a_prompt_convention_not_a_tool_restriction(runtime, tmp_path):
    from dataagent.bootstrap.paths import WorkspaceInput

    source = tmp_path / "inputs"
    source.mkdir()
    target = source / "native-write.txt"
    output = runtime.paths.for_session("sdk").outputs / "result.csv"
    model = ScriptedModel(responses=[
        call("write_file", {"file_path": str(target), "content": "native"}),
        call("write_file", {"file_path": str(output), "content": "ok"}),
        AIMessage(content="Done"),
    ])
    result = await build_graph(runtime, model=model, input_workspaces=(WorkspaceInput("input", source),)).ainvoke({
        "messages": [HumanMessage(content="test")],
    })
    assert target.read_text() == "native"
    assert output.read_text() == "ok"
    assert "execute" in model.tool_schemas[0]
    assert all(item.status == "success" for item in result["messages"] if isinstance(item, ToolMessage))
    prompt = str(model.requests[0][0].content)
    assert "read-only" in prompt.lower() and str(tmp_path) in prompt


async def test_actual_cancellation_reaches_native_callback(runtime):
    ready = asyncio.Event()

    class WaitingModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            ready.set()
            await asyncio.Event().wait()

    hooks = RecordingCallback()
    graph = build_graph(runtime, model=WaitingModel(responses=[])).with_config(callbacks=[hooks])
    task = asyncio.create_task(graph.ainvoke({"messages": [HumanMessage(content="test")]}))
    await asyncio.wait_for(ready.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hooks.events[-1][1] == "on_chain_error"
    assert isinstance(hooks.events[-1][2]["error"], asyncio.CancelledError)
    assert not hooks.active


@pytest.mark.parametrize("child", ["general-purpose"])
async def test_child_model_limit_emits_child_and_root_error(runtime, child):
    runtime = replace(runtime, settings=runtime.settings.model_copy(update={"dataagent": runtime.settings.dataagent.model_copy(update={
        "limits": runtime.settings.dataagent.limits.model_copy(update={"model_calls_per_agent": 1}),
    })}))
    hooks = RecordingCallback()
    model = ScriptedModel(responses=[
        call("task", {"subagent_type": child, "description": "Compute numbers"}),
        call("common__summarize_numbers", {"numbers": [1]}),
    ])
    with pytest.raises(Exception, match="Model call limits exceeded"):
        await build_graph(runtime, model=model).with_config(callbacks=[hooks]).ainvoke({
            "messages": [HumanMessage(content="delegate")],
        })
    assert {agent for agent, event, _ in hooks.events if event == "on_chain_error"} == {child, "dataagent"}
    assert not any(event == "on_chain_end" for _, event, _ in hooks.events)
    assert not hooks.active


async def test_parallel_tool_limit_blocks_dispatch_without_fabricated_tool_events(runtime):
    runtime = replace(runtime, settings=runtime.settings.model_copy(update={"dataagent": runtime.settings.dataagent.model_copy(update={
        "limits": runtime.settings.dataagent.limits.model_copy(update={"tool_calls_per_agent": 1}),
    })}))
    first = call("common__summarize_numbers", {"numbers": [1]})
    second = call("common__summarize_numbers", {"numbers": [2]})
    hooks = RecordingCallback()
    model = ScriptedModel(responses=[AIMessage(content="", tool_calls=first.tool_calls + second.tool_calls)])
    with pytest.raises(Exception, match="Tool call limit"):
        await build_graph(runtime, model=model).with_config(callbacks=[hooks]).ainvoke({"messages": [HumanMessage(content="two tools")]})
    assert [event for _, event, _ in hooks.events] == ["on_chain_start", "on_chain_error"]


async def test_todo_tool_is_real_and_finishes_with_text(runtime):
    model = ScriptedModel(responses=[
        call("write_todos", {"todos": [{"content": "Explain statistics", "status": "completed"}]}),
        AIMessage(content="Explanation complete."),
    ])
    hooks = RecordingCallback()
    result = await build_graph(runtime, model=model).with_config(callbacks=[hooks]).ainvoke({"messages": [HumanMessage(content="Plan a task")]})
    assert result["todos"][0]["status"] == "completed"
    assert result["messages"][-1].content == "Explanation complete."
    assert any(event == "on_tool_end" and name == "write_todos" for name, event, _ in hooks.events)


@pytest.mark.parametrize("child", [None, "general-purpose"])
async def test_tool_limit_is_per_agent_invocation(runtime, child):
    runtime = replace(runtime, settings=runtime.settings.model_copy(update={"dataagent": runtime.settings.dataagent.model_copy(update={
        "limits": runtime.settings.dataagent.limits.model_copy(update={"tool_calls_per_agent": 1}),
    })}))
    tool_call = call("common__summarize_numbers", {"numbers": [1]})
    responses = ([call("task", {"subagent_type": child, "description": "two calls"})] if child else [])
    model = ScriptedModel(responses=[*responses, tool_call, call("common__summarize_numbers", {"numbers": [2]})])
    hooks = RecordingCallback()
    with pytest.raises(Exception, match="Tool call limit"):
        await build_graph(runtime, model=model).with_config(callbacks=[hooks]).ainvoke({"messages": [HumanMessage(content="test")]})
    assert (child or "dataagent", "on_chain_error") in [(name, event) for name, event, _ in hooks.events]
    assert not hooks.active
    model = ScriptedModel(responses=[tool_call, AIMessage(content="one"), tool_call, AIMessage(content="two")])
    graph = build_graph(runtime, model=model, checkpointer=InMemorySaver())
    for _ in range(2):
        await graph.ainvoke({"messages": [HumanMessage(content="test")]}, {"configurable": {"thread_id": "repeat"}})


async def test_parallel_calls_and_concurrent_threads_have_distinct_scopes(runtime):
    class ParallelModel(ScriptedModel):
        async def _agenerate(self, messages, *args, **kwargs):
            if isinstance(messages[-1], HumanMessage):
                return self._generate(messages, *args, **kwargs)
            from langchain_core.outputs import ChatGeneration, ChatResult
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="done"))])

    calls = [call("common__summarize_numbers", {"numbers": [number]}, id=f"call-{number}") for number in [1, 2]]
    model = ParallelModel(responses=[AIMessage(content="", tool_calls=[item.tool_calls[0] for item in calls])] * 2)
    hooks = RecordingCallback()
    graph = build_graph(runtime, model=model, checkpointer=InMemorySaver()).with_config(callbacks=[hooks])
    await asyncio.gather(*[
        graph.ainvoke({"messages": [HumanMessage(content="test")]}, {"configurable": {"thread_id": thread}})
        for thread in ["one", "two"]
    ])
    completed_calls = [details["output"].tool_call_id for _, event, details in hooks.events if event == "on_tool_end"]
    assert sorted(completed_calls) == ["call-1", "call-1", "call-2", "call-2"]
    roots = {details["thread_id"]: details["run_id"] for _, event, details in hooks.events
             if event == "on_chain_start"}
    assert set(roots) == {"one", "two"} and len(set(roots.values())) == 2
    tool_runs = [details["run_id"] for _, event, details in hooks.events if event == "on_tool_start"]
    assert len(set(tool_runs)) == 4
    assert all(details["thread_id"] in roots for _, _, details in hooks.events)
    assert not hooks.active


async def test_checkpoint_initialization_failure_has_error_without_before(runtime):
    class BrokenSaver(InMemorySaver):
        async def aget_tuple(self, config):
            raise RuntimeError("Checkpoint initialization failed")
    hooks = RecordingCallback()
    with pytest.raises(RuntimeError, match="Checkpoint initialization failed"):
        await build_graph(runtime, model=ScriptedModel(responses=[]), checkpointer=BrokenSaver()).with_config(callbacks=[hooks]).ainvoke(
            {"messages": [HumanMessage(content="test")]}, {"configurable": {"thread_id": "broken"}},
        )
    assert [event for _, event, _ in hooks.events] == ["on_chain_start", "on_chain_error"]
    assert not hooks.active


@pytest.mark.parametrize("event", ["before_agent", "before_model", "after_model", "before_tool", "after_tool", "after_agent"])
async def test_compiled_hook_exceptions_propagate_without_swallowing_or_retry(runtime, tmp_path, event):
    import json

    root = tmp_path / "policy"
    root.mkdir()
    signature = ("request, result" if event == "after_tool" else "request"
                 if event == "before_tool" else "state, runtime")
    (root / "policy.py").write_text(
        f"async def handle({signature}, *, params):\n    raise RuntimeError('Policy failure')\n"
    )
    (root / ".plugin.json").write_text(json.dumps({
        "schema_version": 3, "id": "policy",
        "hooks": [{"entrypoint": "policy.py:handle", "event": event}],
    }))
    runtime = replace(runtime, settings=runtime.settings.model_copy(update={"plugins": runtime.settings.plugins.model_copy(update={
        "enabled": ["common", "policy"], "paths": [root],
    })}))
    hooks = RecordingCallback()
    model = ScriptedModel(responses=[
        call("common__summarize_numbers", {"numbers": [1]}), AIMessage(content="Original successful answer"),
    ])
    graph = build_graph(runtime, model=model).with_config(callbacks=[hooks])
    with pytest.raises(RuntimeError, match="Policy failure"):
        await graph.ainvoke({"messages": [HumanMessage(content="test")]})
    assert hooks.events[-1][1] == "on_chain_error"
    assert not any(event == "on_chain_end" for _, event, _ in hooks.events)
    assert len(model.requests) == (0 if event in {"before_agent", "before_model"}
                                   else 2 if event == "after_agent" else 1)
    assert not hooks.active


def _with_entry(runtime, entry, **fields):
    dataagent = runtime.settings.dataagent.model_copy(update={"entry": entry, **fields})
    settings = runtime.settings.model_copy(update={"dataagent": dataagent})
    return replace(runtime, settings=settings)


def test_unknown_entry_lists_compiled_subagents(runtime):
    with pytest.raises(ValueError, match="missing") as error:
        build_graph(_with_entry(runtime, "missing"), model=ScriptedModel(responses=[]))
    message = str(error.value)
    assert "Available compiled subagents: (none)" in message
    assert "general-purpose" not in message


def test_standard_subagent_cannot_be_an_entry(runtime):
    with pytest.raises(ValueError, match="general-purpose") as error:
        build_graph(_with_entry(runtime, "general-purpose"), model=ScriptedModel(responses=[]))
    assert "only compiled subagents can be entries" in str(error.value)


def _write_direct_plugin(root: Path):
    (root / "prompts").mkdir(parents=True)
    (root / "prompts" / "agent.md").write_text("Route work to specialists.\n", encoding="utf-8")
    (root / "prompts" / "notes.md").write_text("Take notes.\n", encoding="utf-8")
    skill = root / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Unused when this plugin is the entry.\n---\n\n# Demo\n",
        encoding="utf-8",
    )
    (root / "tools.py").write_text(
        'def ping() -> str:\n    """Return a ping."""\n    return "pong"\n',
        encoding="utf-8",
    )
    (root / "subagents").mkdir()
    (root / "subagents" / "direct.json").write_text(json.dumps({
        "kind": "compiled", "name": "direct", "description": "Compiled entry.",
        "entrypoint": "subagents/direct.py:build",
    }))
    (root / "subagents" / "notes.json").write_text(json.dumps({
        "name": "notes", "description": "Standard sibling.", "system_prompt_file": "prompts/notes.md",
    }))
    (root / "subagents" / "direct.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "from langchain_core.messages import AIMessage\n"
        "from langchain_core.runnables import RunnableLambda\n"
        "\n"
        "def build(params, services):\n"
        "    seen = services.get_model()\n"
        "    seen_none = services.get_model(None)\n"
        "    Path(params['evidence']).write_text(json.dumps({\n"
        "        'model_id': id(seen),\n"
        "        'none_alias_id': id(seen_none),\n"
        "        'disable_streaming': seen.disable_streaming,\n"
        "    }), encoding='utf-8')\n"
        "\n"
        "    def run(state):\n"
        "        text = state['messages'][-1].content\n"
        "        return {'messages': [AIMessage(content=f'reply:{text}')]}\n"
        "\n"
        "    return RunnableLambda(run)\n",
        encoding="utf-8",
    )
    (root / ".plugin.json").write_text(json.dumps({
        "id": "direct",
        "system_prompt_file": "prompts/agent.md",
        "tools": {"ping": {"entrypoint": "tools.py:ping"}},
        "skills": ["skills"],
        "subagents": ["subagents/direct.json", "subagents/notes.json"],
    }))


def _select_home(runtime, tmp_path, body):
    (runtime.paths.home / "config.json").write_text(json.dumps({
        "models": {"primary": {
            "name": "test-model", "base_url": "https://example.invalid/v1", "api_key": "test-secret",
        }},
        **body,
    }))
    return prepare_runtime(LaunchOptions(cwd=tmp_path))


def _runtime_with_entry(runtime, tmp_path, entry):
    _write_direct_plugin(tmp_path / "home" / "plugins" / "direct")
    evidence = tmp_path / "evidence.json"
    prepared = _select_home(runtime, tmp_path, {
        "plugins": {"enabled": ["direct"], "config": {"direct": {"evidence": str(evidence)}}},
        "dataagent": {"entry": entry},
    })
    return prepared, evidence


async def test_entry_leaves_root_settings_unmounted(runtime, tmp_path):
    _write_direct_plugin(tmp_path / "home" / "plugins" / "direct")
    evidence = tmp_path / "evidence.json"
    marker = tmp_path / "hook-ran"
    workspace = tmp_path / "input"
    workspace.mkdir()
    hooks = tmp_path / "home" / "hooks"
    hooks.mkdir()
    (hooks / "mark.py").write_text(
        "from pathlib import Path\n"
        "\n"
        "def handle(state, runtime, *, params):\n"
        "    Path(params['marker']).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    prepared = _select_home(runtime, tmp_path, {
        "plugins": {"enabled": ["direct"], "config": {"direct": {"evidence": str(evidence)}}},
        "dataagent": {
            "entry": "direct",
            "limits": {"model_calls_per_agent": 3, "tool_calls_per_agent": 4},
            "workspaces": [{"name": "input", "path": str(workspace), "read_only": True}],
            "hooks": {"before_agent": [{
                "entrypoint": "hooks/mark.py:handle",
                "params": {"marker": str(marker)},
            }]},
        },
    })
    assert prepared.settings.dataagent.limits.model_calls_per_agent == 3
    assert prepared.settings.dataagent.workspaces[0].name == "input"
    assert prepared.settings.dataagent.hooks
    graph = build_graph(prepared, model=ScriptedModel(responses=[]))
    assert set(graph.nodes) == {"__start__", "direct"}
    result = await graph.ainvoke({"messages": [HumanMessage(content="ping")]})
    assert result["messages"][-1].content == "reply:ping"
    assert not marker.exists()


def test_unknown_entry_fails_before_a_broken_factory(runtime, tmp_path):
    root = tmp_path / "home" / "plugins" / "direct"
    root.mkdir(parents=True)
    (root / "subagents").mkdir()
    (root / ".plugin.json").write_text(json.dumps({
        "id": "direct", "subagents": ["subagents/direct.json"],
    }))
    (root / "subagents" / "direct.json").write_text(json.dumps({
        "kind": "compiled", "name": "direct", "description": "Factory must not run.",
        "entrypoint": "subagents/direct.py:build",
    }))
    (root / "subagents" / "direct.py").write_text(
        "def build(params, services):\n"
        "    raise RuntimeError('factory exploded')\n",
        encoding="utf-8",
    )
    prepared = _select_home(runtime, tmp_path, {
        "plugins": {"enabled": ["direct"]},
        "dataagent": {"entry": "missing"},
    })
    with pytest.raises(ValueError, match="missing") as error:
        build_graph(prepared, model=ScriptedModel(responses=[]))
    message = str(error.value)
    assert "Available compiled subagents: direct" in message
    assert "factory exploded" not in message


def test_unknown_compiled_name_lists_only_compiled_subagents(runtime, tmp_path):
    prepared, _evidence = _runtime_with_entry(runtime, tmp_path, "missing")
    with pytest.raises(ValueError, match="missing") as error:
        build_graph(prepared, model=ScriptedModel(responses=[]))
    message = str(error.value)
    assert "Available compiled subagents: direct" in message
    assert "notes" not in message


async def test_entry_graph_checkpoints_and_keeps_the_cached_model(runtime, tmp_path):
    prepared, evidence = _runtime_with_entry(runtime, tmp_path, "direct")
    dataagent = prepared.settings.dataagent.model_copy(update={
        "limits": prepared.settings.dataagent.limits.model_copy(update={"timeout_seconds": 30}),
    })
    prepared = replace(prepared, settings=prepared.settings.model_copy(update={"dataagent": dataagent}))
    model = ScriptedModel(responses=[])
    context = AgentContext(prepared, model=model)
    graph = build_graph(prepared, context=context, checkpointer=InMemorySaver())
    assert isinstance(graph, CompiledStateGraph)
    assert graph.config["recursion_limit"] == 9999
    assert set(graph.nodes) == {"__start__", "direct"}
    assert set(graph.get_graph().nodes) == {"__start__", "direct", "__end__"}
    assert context.get_model() is model and model.disable_streaming is False
    assert json.loads(evidence.read_text(encoding="utf-8")) == {
        "model_id": id(model),
        "none_alias_id": id(model),
        "disable_streaming": False,
    }
    config = {"configurable": {"thread_id": "persist"}}
    for index, text in enumerate(("first", "second"), start=1):
        await graph.ainvoke({"messages": [HumanMessage(content=text)]}, config)
        state = await graph.aget_state(config)
        assert state.next == ()
        assert set(state.values) == {"messages"}
        assert len(state.values["messages"]) == index * 2
    assert [message.content for message in state.values["messages"]] == [
        "first", "reply:first", "second", "reply:second",
    ]
