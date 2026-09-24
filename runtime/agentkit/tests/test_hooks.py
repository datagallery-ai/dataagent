"""Configured hooks on real graphs; runner edge cases live in test_native_hooks."""

import asyncio
import json
from dataclasses import replace

import pytest
from conftest import RecordingCallback, ScriptedModel, call
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from dataagent.agent import build_agent

EVENTS = ["before_agent", "before_model", "after_model", "before_tool", "after_tool", "after_agent"]


def configured_hooks(runtime, tmp_path, *, child=None, events=EVENTS):
    root = tmp_path / "trace"
    root.mkdir()
    output = root / "events.jsonl"
    (root / "record.py").write_text(
        "import json\nfrom pathlib import Path\n"
        "from langchain_core.messages import BaseMessage\n"
        "def record(params, **details):\n"
        "    with Path(params['output']).open('a') as stream:\n"
        "        stream.write(json.dumps({'event': params['event'], 'scope': params['scope'], **details}) + '\\n')\n"
        "async def state_hook(state, runtime, *, params):\n"
        "    assert isinstance(state['messages'][-1], BaseMessage)\n"
        "    assert hasattr(runtime, 'context')\n"
        "    record(params, message_type=state['messages'][-1].type)\n"
        "async def before_tool(request, *, params):\n"
        "    record(params, tool=request.tool_call['name'], call_id=request.tool_call['id'])\n"
        "async def after_tool(request, result, *, params):\n"
        "    record(params, tool=request.tool_call['name'], call_id=request.tool_call['id'],\n"
        "           status=getattr(result, 'status', None))\n"
    )

    def hooks(scope):
        return [{"event": event, "entrypoint": f"record.py:{event if event in {'before_tool', 'after_tool'} else 'state_hook'}",
                 "params": {"output": str(output), "event": event, "scope": scope}}
                for event in events]

    spec = {"schema_version": 2, "id": "trace", "hooks": hooks("dataagent-v2")}
    if child:
        (root / "child.md").write_text("Compute the delegated statistics with tools.")
        (root / "child.json").write_text(json.dumps({
            "name": child, "description": "A statistics delegate", "system_prompt": "child.md",
            "tools": ["common__summarize_numbers"], "hooks": hooks(child),
        }))
        spec["subagents"] = ["child.json"]
    (root / ".plugin.json").write_text(json.dumps(spec))
    runtime = replace(runtime, settings=runtime.settings.model_copy(update={
        "plugins": runtime.settings.plugins.model_copy(update={"enabled": ["common", "trace"], "paths": [root]}),
    }))
    return runtime, output


def recorded(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("scenario", ["success", "tool_error", "model_error"])
async def test_configured_hooks_follow_native_lifecycle(runtime, tmp_path, scenario):
    runtime, output = configured_hooks(runtime, tmp_path)
    responses = [] if scenario == "model_error" else [
        call("common__summarize_numbers", {"numbers": "wrong-type" if scenario == "tool_error" else [1, 2]}),
        AIMessage(content="finished"),
    ]
    recorder = RecordingCallback()
    graph = build_agent(runtime, model=ScriptedModel(responses=responses)).with_config(callbacks=[recorder])
    if scenario == "model_error":
        with pytest.raises(RuntimeError, match="ran out"):
            await graph.ainvoke({"messages": [HumanMessage(content="test")]})
    else:
        result = await graph.ainvoke({"messages": [HumanMessage(content="test")]})
        assert result["messages"][-1].content == "finished"
    values = recorded(output)
    expected = ["before_agent", "before_model"]
    if scenario == "model_error":
        assert recorder.events[-1][1] == "on_chain_error"
    else:
        expected += ["after_model", "before_tool", "after_tool",
                     "before_model", "after_model", "after_agent"]
        after_tool = next(item for item in values if item["event"] == "after_tool")
        assert after_tool["status"] == ("error" if scenario == "tool_error" else "success")
        assert recorder.events[-1][1] == "on_chain_end"
    assert [item["event"] for item in values] == expected
    assert {item["scope"] for item in values} == {"dataagent-v2"}
    assert not recorder.active


async def test_explicit_child_hooks_keep_native_scope(runtime, tmp_path):
    runtime, output = configured_hooks(runtime, tmp_path, child="traced-statistics")
    model = ScriptedModel(responses=[
        call("task", {"subagent_type": "traced-statistics", "description": "Compute 1,2"}),
        call("common__summarize_numbers", {"numbers": [1, 2]}),
        AIMessage(content="child result"), AIMessage(content="root result"),
    ])
    recorder = RecordingCallback(agents=("dataagent-v2", "traced-statistics"))
    graph = build_agent(runtime, model=model).with_config(callbacks=[recorder])
    await graph.ainvoke({"messages": [HumanMessage(content="delegate")]},
                       {"configurable": {"thread_id": "scoped"}})
    values = recorded(output)
    assert [item["scope"] for item in values if item["event"] == "before_agent"] == ["dataagent-v2", "traced-statistics"]
    tools = [(item["scope"], item["tool"]) for item in values if item["event"] == "before_tool"]
    assert tools == [("dataagent-v2", "task"), ("traced-statistics", "common__summarize_numbers")]
    terminals = [item for item in values if item["event"] == "after_agent"]
    assert [item["scope"] for item in terminals] == ["traced-statistics", "dataagent-v2"]
    completed = [details for _, event, details in recorder.events if event == "on_chain_end"]
    assert len(completed) == 2 and len({item["run_id"] for item in completed}) == 2
    assert completed[0]["parent_run_id"] is not None
    assert completed[1]["parent_run_id"] is None
    assert {item["thread_id"] for item in completed} == {"scoped"}
    assert not recorder.active


async def test_root_hooks_do_not_implicitly_apply_to_general_purpose(runtime, tmp_path):
    runtime, output = configured_hooks(runtime, tmp_path)
    model = ScriptedModel(responses=[
        call("task", {"subagent_type": "general-purpose", "description": "Compute 1"}),
        call("common__summarize_numbers", {"numbers": [1]}),
        AIMessage(content="child result"), AIMessage(content="root result"),
    ])
    recorder = RecordingCallback()
    graph = build_agent(runtime, model=model).with_config(callbacks=[recorder])
    await graph.ainvoke({"messages": [HumanMessage(content="delegate")]})
    values = recorded(output)
    assert {item["scope"] for item in values} == {"dataagent-v2"}
    assert [item["tool"] for item in values if item["event"] == "before_tool"] == ["task"]
    assert ("general-purpose", "on_chain_end") in [(name, event) for name, event, _ in recorder.events]
    assert not recorder.active


async def test_sdk_run_name_override_does_not_change_after_agent_hook(runtime, tmp_path):
    runtime, output = configured_hooks(runtime, tmp_path, events=["after_agent"])
    graph = build_agent(runtime, model=ScriptedModel(responses=[AIMessage(content="done")]))
    await graph.ainvoke({"messages": [HumanMessage(content="hello")]}, {"run_name": "sdk-custom-name"})
    assert [item["event"] for item in recorded(output)] == ["after_agent"]


async def test_initialization_failure_only_reaches_native_error_callback(runtime, tmp_path):
    runtime, output = configured_hooks(runtime, tmp_path)

    class BrokenSaver(InMemorySaver):
        async def aget_tuple(self, config):
            raise RuntimeError("Checkpoint initialization failed")

    recorder = RecordingCallback()
    graph = build_agent(runtime, model=ScriptedModel(responses=[]), checkpointer=BrokenSaver()).with_config(callbacks=[recorder])
    with pytest.raises(RuntimeError, match="Checkpoint initialization failed"):
        await graph.ainvoke({"messages": [HumanMessage(content="test")]},
                           {"configurable": {"thread_id": "broken"}})
    assert recorded(output) == []
    assert [event for _, event, _ in recorder.events] == ["on_chain_start", "on_chain_error"]
    assert not recorder.active


async def test_cancellation_does_not_run_native_after_agent_hook(runtime, tmp_path):
    runtime, output = configured_hooks(runtime, tmp_path)
    ready = asyncio.Event()

    class WaitingModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            ready.set()
            await asyncio.Event().wait()

    recorder = RecordingCallback()
    graph = build_agent(runtime, model=WaitingModel(responses=[])).with_config(callbacks=[recorder])
    task = asyncio.create_task(graph.ainvoke({"messages": [HumanMessage(content="test")]}))
    await asyncio.wait_for(ready.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    values = recorded(output)
    assert [item["event"] for item in values] == ["before_agent", "before_model"]
    assert recorder.events[-1][1] == "on_chain_error"
    assert isinstance(recorder.events[-1][2]["error"], asyncio.CancelledError)
    assert not recorder.active


def test_safe_error_preserves_connection_cause_and_redacts_credentials():
    from dataagent import safe_error

    cause = RuntimeError("Server disconnected; credential=private-key")
    error = ConnectionError("Connection error.")
    error.__cause__ = cause
    result = safe_error(error, ("private-key",))
    assert result["code"] == "ConnectionError"
    assert "Server disconnected" in result["message"]
    assert "private-key" not in result["message"]


def test_safe_error_preserves_empty_causes_and_honors_suppressed_context():
    from dataagent import safe_error

    error = ConnectionError("Connection error.")
    error.__cause__ = OSError()
    error.__cause__.__context__ = TimeoutError("credential=private-key")
    message = safe_error(error, ("private-key",))["message"]
    assert "OSError" in message and "TimeoutError" in message
    assert "private-key" not in message
    error.__cause__.__suppress_context__ = True
    assert "TimeoutError" not in safe_error(error)["message"]
    error.__cause__.__cause__ = error
    assert len(safe_error(error)["message"]) < 100  # Cycles terminate.
