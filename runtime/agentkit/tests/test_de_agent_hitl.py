"""Checkpoint/restart, native tool results, policy budgets and HITL configuration."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from dataagent.core.graph import create_deep_agent
from dataagent.declarations import HookSpec, PluginSpec
from dataagent.extensions.compiler import compile_extensions
from dataagent.extensions.hooks import compile_hook
from dataagent.extensions.loading import PythonLoader

ROOT = Path(__file__).resolve().parents[1] / "builtin-plugins/de-agent"
NAME = "de-agent__request_human_feedback"
ARGS = {"reason": "统计标识不明确", "pending_action": "倾向用 user_id，还是 device_id？"}


def harness(tmp_path, responses, *, config=None, saver=None, loader=None):
    loader = loader or PythonLoader()
    function = loader.load(ROOT, "tools/hitl.py:request_human_feedback")
    prepare = loader.load(ROOT, "hooks/hitl.py:prepare_context")
    guard = loader.load(ROOT, "hooks/hitl.py:guard_questions")
    options = {"hitl": {"enabled": True, **(config or {})}}
    states = {}
    ctx = SimpleNamespace(
        session=SimpleNamespace(root=tmp_path, outputs=tmp_path),
        get_plugin_config=lambda _: options,
        get_plugin_state=lambda _: states,
    )
    model = ScriptedModel(responses=responses)
    middleware = [
        compile_hook(HookSpec(event=event, entrypoint="unused.py:handle"), handler, name=event)
        for event, handler in (("before_model", prepare), ("after_model", guard))
    ]
    graph = create_deep_agent(
        model=model,
        tools=[tool(function).model_copy(update={"name": NAME})],
        middleware=middleware,
        checkpointer=saver or InMemorySaver(),
        context_schema=SimpleNamespace,
    )
    return graph, ctx, function, loader


async def test_human_interrupt_survives_rebuilt_graph_and_counts_once(tmp_path):
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "task"}}
    graph, ctx, _, _ = harness(tmp_path, [call(NAME, ARGS, "ask")], saver=saver)
    first = await graph.ainvoke(
        {"messages": [HumanMessage(content="统计用户行为")]}, config, context=ctx
    )
    assert first["__interrupt__"][0].value["pending_action"] == ARGS["pending_action"]
    snapshot = await graph.aget_state(config)
    assert snapshot.values["hitl"]["count"] == 0
    rebuilt, ctx, _, _ = harness(tmp_path, [AIMessage(content="按 user_id 继续")], saver=saver)
    result = await rebuilt.ainvoke(Command(resume="使用 user_id"), config, context=ctx)
    answers = [m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == NAME]
    assert len(answers) == 1 and answers[0].tool_call_id == "ask"
    assert answers[0].response_metadata["hitl"]["source"] == "human"
    assert (await rebuilt.aget_state(config)).values["hitl"]["count"] == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_tool_is_not_exposed_to_model(tmp_path, enabled):
    spec = PluginSpec.model_validate(
        {
            "id": "de-agent",
            "tools": {
                "request_human_feedback": {
                    "entrypoint": "tools/hitl.py:request_human_feedback",
                    "enabled_when": "hitl.enabled",
                }
            },
        }
    )
    result = compile_extensions(
        [(ROOT, spec)], plugin_params={"de-agent": {"hitl": {"enabled": enabled}}}
    )
    assert [t.name for t in result["tools"]] == ([NAME] if enabled else [])


async def test_dedup_and_budget_do_not_interrupt_or_claim_user_confirmation(tmp_path):
    graph, ctx, function, _ = harness(tmp_path, [])
    runtime = SimpleNamespace(
        context=ctx,
        state={
            "hitl": {
                "count": 4,
                "budget": 4,
                "history": [],
                "field_asks": {},
                "fingerprint_asks": {},
            }
        },
        tool_call_id="limit",
    )
    result = await function(**ARGS, runtime=runtime)
    message = result.update["messages"][0]
    assert "预算上限" in message.content
    assert message.response_metadata["hitl"] == {"source": "policy", "action": "BUDGET_EXHAUSTED"}
    runtime.state = result.update
    runtime.state["hitl"]["count"] = 1
    runtime.tool_call_id = "repeat"
    result = await function(**ARGS, runtime=runtime)
    assert result.update["messages"][0].content == message.content


async def test_evaluation_uses_current_task_golden_without_interrupt(tmp_path):
    golden = tmp_path / "golden.sql"
    golden.write_text(
        "INSERT OVERWRITE db.result SELECT user_id, COUNT(1) FROM db.correct GROUP BY user_id"
    )
    args = {"reason": "源表确认", "pending_action": "倾向使用 db.wrong 作为源表，请确认所选源表？"}
    graph, ctx, _, _ = harness(
        tmp_path,
        [
            call(NAME, args, "auto"),
            AIMessage(content="继续检索"),
        ],
        config={"auto_resolve": True, "golden_sql_path": str(golden), "eval_task_name": "task"},
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="统计用户行为")]},
        {"configurable": {"thread_id": "eval"}},
        context=ctx,
    )
    assert "__interrupt__" not in result
    answer = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == NAME)
    assert "db.correct" not in answer.content
    assert answer.response_metadata["hitl"]["source"] == "evaluation"
    data = (await graph.aget_state({"configurable": {"thread_id": "eval"}})).values["hitl"]
    assert data["count"] == 1
    assert str(golden) not in json.dumps(data)


async def test_unattended_text_question_is_converted_once(tmp_path):
    graph, ctx, _, _ = harness(
        tmp_path,
        [AIMessage(content="请确认使用 user_id 还是 device_id？"), AIMessage(content="已完成")],
        config={"auto_resolve": True},
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="统计")]},
        {"configurable": {"thread_id": "text"}},
        context=ctx,
    )
    assert result["messages"][-1].content == "已完成"
    assert (await graph.aget_state({"configurable": {"thread_id": "text"}})).values["hitl"][
        "text_guard_fires"
    ] == 1


async def test_evaluation_timeout_is_policy_fallback_not_confirmation(tmp_path, monkeypatch):
    _, ctx, function, _ = harness(tmp_path, [], config={"auto_resolve": True})
    module = sys.modules[function.__module__]
    monkeypatch.setattr(module, "auto_answer", AsyncMock(side_effect=TimeoutError))
    result = await function(
        **ARGS, runtime=SimpleNamespace(context=ctx, state={}, tool_call_id="timeout")
    )
    assert result.update["messages"][0].response_metadata["hitl"]["action"] == "FALLBACK"


async def test_evaluation_model_bridge_cancels_native_stream(tmp_path):
    import asyncio

    loader = PythonLoader()
    cls = loader.load(ROOT, "utils/hitl.py:EvaluationRuntime")
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def stream(messages):
        started.set()
        try:
            await asyncio.Event().wait()
            yield AIMessage(content="unreachable")
        finally:
            cancelled.set()

    ctx = SimpleNamespace(
        session=SimpleNamespace(root=tmp_path, outputs=tmp_path),
        get_plugin_config=lambda _: {},
        get_model=lambda _: SimpleNamespace(astream=stream),
    )
    adapter = cls(ctx, {}, {})
    task = asyncio.create_task(asyncio.to_thread(adapter.llm("primary").invoke, []))
    await asyncio.wait_for(started.wait(), 2)
    adapter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cancelled.wait(), 2)
