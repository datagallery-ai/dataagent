import asyncio
import json
from dataclasses import replace

import pytest
from conftest import RecordingCallback, ScriptedModel, call
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from dataagent.agent import build_agent
from dataagent.declarations import HookSpec


def test_build_returns_native_graph_without_callback_adapter(runtime):
    graph = build_agent(runtime, model=ScriptedModel(responses=[]))
    assert isinstance(graph, CompiledStateGraph)
    with pytest.raises(TypeError, match="callbacks"):
        build_agent(runtime, model=ScriptedModel(responses=[]), callbacks=[])


def test_independent_hooks_without_bindings_fail_explicitly(runtime):
    settings = runtime.settings.model_copy(update={
        "dataagent": runtime.settings.dataagent.model_copy(update={
            "hooks": (HookSpec(event="before_agent", entrypoint="missing.py:handle"),),
        }),
    })
    with pytest.raises(ValueError, match="Hook bindings do not match"):
        build_agent(replace(runtime, settings=settings), model=ScriptedModel(responses=[]))


async def test_builtin_audit_uses_six_hooks_without_logging_payloads(runtime, caplog):
    caplog.set_level("INFO", logger="dataagent.audit")
    model = ScriptedModel(responses=[
        call("common__summarize_numbers", {"numbers": [98765]}), AIMessage(content="private-answer"),
    ])
    await build_agent(runtime, model=model).ainvoke({"messages": [HumanMessage(content="private-question")]})
    records = [record.getMessage() for record in caplog.records if record.name == "dataagent.audit"]
    assert [record.split()[0] for record in records] == [
        "hook.before_agent", "hook.before_model", "hook.after_model", "hook.before_tool",
        "hook.after_tool", "hook.before_model", "hook.after_model", "hook.after_agent",
    ]
    assert all("agent=dataagent-v2" in record for record in records)
    assert not any(value in "\n".join(records) for value in ("private-question", "private-answer", "98765", "test-secret"))


async def test_real_tool_and_sdk_hooks(runtime):
    model = ScriptedModel(responses=[
        call("common__summarize_numbers", {"numbers": [1, 2, 3]}), AIMessage(content="Mean: 2"),
    ])
    hooks = RecordingCallback()
    graph = build_agent(runtime, model=model).with_config(callbacks=[hooks])
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
    result = await build_agent(runtime, model=model).with_config(callbacks=[hooks]).ainvoke({
        "messages": [HumanMessage(content="Delegate the statistics")],
    })
    assert result["messages"][-1].content == "Mean: 2"
    assert [(name, event) for name, event, _ in hooks.events if event == "on_tool_end"] == [
        ("read_file", "on_tool_end"), ("common__summarize_numbers", "on_tool_end"),
        ("task", "on_tool_end"),
    ]
    scopes = {agent: details for agent, event, details in hooks.events if event == "on_chain_start"}
    root, child = scopes["dataagent-v2"], scopes["general-purpose"]
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
    await build_agent(runtime, model=model).ainvoke({"messages": [HumanMessage(content="Inspect CSV workflow")]})
    assert any(isinstance(message, ToolMessage) and "# Tabular inspection" in str(message.content)
               for request in model.requests for message in request)
    assert "execute" in model.tool_schemas[1]


@pytest.mark.parametrize("args", [{"numbers": []}, {"numbers": "wrong-type"}])
async def test_error_tool_message_is_observed_not_false_success(runtime, args):
    hooks = RecordingCallback()
    model = ScriptedModel(responses=[
        call("common__summarize_numbers", args), AIMessage(content="Please provide numbers"),
    ])
    result = await build_agent(runtime, model=model).with_config(callbacks=[hooks]).ainvoke({
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
        await build_agent(runtime, model=model).with_config(callbacks=[hooks]).ainvoke({
            "messages": [HumanMessage(content="test")],
        })
    assert len(model.requests) == 1
    assert hooks.events[-1][1] == "on_chain_error"
    assert not hooks.active


@pytest.mark.parametrize("child", [False, True])
async def test_default_limits_allow_128_model_and_tool_calls(runtime, child):
    limits = runtime.settings.dataagent.limits
    assert limits.model_calls_per_agent == limits.tool_calls_per_agent == 128
    calls = [call("common__summarize_numbers", {"numbers": [1]}, id=f"stats-{index}")
             for index in range(128)]
    # Two tools in the first model response leave room for the final model answer.
    responses = [AIMessage(content="", tool_calls=calls[0].tool_calls + calls[1].tool_calls),
                 *calls[2:], AIMessage(content="done")]
    if child:
        responses = [call("task", {"subagent_type": "general-purpose", "description": "Compute statistics"}),
                     *responses, AIMessage(content="done")]
    model = ScriptedModel(responses=responses)
    graph = build_agent(runtime, model=model)
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
    graph = build_agent(runtime, model=model, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "persist"}}
    for text in ["first", "second"]:
        result = await graph.ainvoke({"messages": [HumanMessage(content=text)]}, config)
    assert len(result["messages"]) == 4
    model = ScriptedModel(responses=[call("common__summarize_numbers", {"numbers": [1]})])
    with pytest.raises(Exception, match="Model call limits exceeded"):
        await build_agent(runtime, model=model).ainvoke({"messages": [HumanMessage(content="test")]})


async def test_workspace_readonly_is_a_prompt_convention_not_a_tool_restriction(runtime, tmp_path):
    from dataagent.bootstrap.paths import WorkspaceInput

    target = tmp_path / "native-write.txt"
    output = runtime.paths.for_session("sdk").outputs / "result.csv"
    model = ScriptedModel(responses=[
        call("write_file", {"file_path": str(target), "content": "native"}),
        call("write_file", {"file_path": str(output), "content": "ok"}),
        AIMessage(content="Done"),
    ])
    result = await build_agent(runtime, model=model, input_workspaces=(WorkspaceInput("input", tmp_path),)).ainvoke({
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
    graph = build_agent(runtime, model=WaitingModel(responses=[])).with_config(callbacks=[hooks])
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
        await build_agent(runtime, model=model).with_config(callbacks=[hooks]).ainvoke({
            "messages": [HumanMessage(content="delegate")],
        })
    assert {agent for agent, event, _ in hooks.events if event == "on_chain_error"} == {child, "dataagent-v2"}
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
        await build_agent(runtime, model=model).with_config(callbacks=[hooks]).ainvoke({"messages": [HumanMessage(content="two tools")]})
    assert [event for _, event, _ in hooks.events] == ["on_chain_start", "on_chain_error"]


async def test_todo_tool_is_real_and_finishes_with_text(runtime):
    model = ScriptedModel(responses=[
        call("write_todos", {"todos": [{"content": "Explain statistics", "status": "completed"}]}),
        AIMessage(content="Explanation complete."),
    ])
    hooks = RecordingCallback()
    result = await build_agent(runtime, model=model).with_config(callbacks=[hooks]).ainvoke({"messages": [HumanMessage(content="Plan a task")]})
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
        await build_agent(runtime, model=model).with_config(callbacks=[hooks]).ainvoke({"messages": [HumanMessage(content="test")]})
    assert (child or "dataagent-v2", "on_chain_error") in [(name, event) for name, event, _ in hooks.events]
    assert not hooks.active
    model = ScriptedModel(responses=[tool_call, AIMessage(content="one"), tool_call, AIMessage(content="two")])
    graph = build_agent(runtime, model=model, checkpointer=InMemorySaver())
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
    graph = build_agent(runtime, model=model, checkpointer=InMemorySaver()).with_config(callbacks=[hooks])
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
        await build_agent(runtime, model=ScriptedModel(responses=[]), checkpointer=BrokenSaver()).with_config(callbacks=[hooks]).ainvoke(
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
        "schema_version": 2, "id": "policy",
        "hooks": [{"entrypoint": "policy.py:handle", "event": event}],
    }))
    runtime = replace(runtime, settings=runtime.settings.model_copy(update={"plugins": runtime.settings.plugins.model_copy(update={
        "enabled": ["common", "policy"], "paths": [root],
    })}))
    hooks = RecordingCallback()
    model = ScriptedModel(responses=[
        call("common__summarize_numbers", {"numbers": [1]}), AIMessage(content="Original successful answer"),
    ])
    graph = build_agent(runtime, model=model).with_config(callbacks=[hooks])
    with pytest.raises(RuntimeError, match="Policy failure"):
        await graph.ainvoke({"messages": [HumanMessage(content="test")]})
    assert hooks.events[-1][1] == "on_chain_error"
    assert not any(event == "on_chain_end" for _, event, _ in hooks.events)
    assert len(model.requests) == (0 if event in {"before_agent", "before_model"}
                                   else 2 if event == "after_agent" else 1)
    assert not hooks.active
