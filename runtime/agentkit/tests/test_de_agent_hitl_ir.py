"""IR→HITL blocks consumers, absorbs answers and releases unresolved fields finitely."""

import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_de_agent_ir_session as ir_fixtures
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from dataagent.core.graph import create_deep_agent
from dataagent.declarations import HookSpec
from dataagent.extensions.hooks import compile_hook
from dataagent.extensions.loading import PythonLoader

ir_h = ir_fixtures.h

ROOT = Path(__file__).resolve().parents[1] / "builtin-plugins/de-agent"
FIELD = {
    "field_id": "identity",
    "value": {},
    "unresolved": ["统计标识缺失"],
    "question": "用 user_id 还是 device_id？",
}


def setup(tmp_path):
    loader = PythonLoader()
    guard = loader.load(ROOT, "hooks/hitl.py:guard_questions")
    module = sys.modules[guard.__module__]
    data = {"data_task_ir": {"ir_field_values": [deepcopy(FIELD)]}}
    config = {"hitl": {"enabled": True}, "data_task_ir": {"enabled": True}}
    context = SimpleNamespace(
        get_plugin_config=lambda _: config,
        get_plugin_state=lambda _: data,
        session=SimpleNamespace(root=tmp_path, outputs=tmp_path),
    )
    return loader, guard, module, context, data, config


async def test_consumer_is_not_executed_until_answer_and_does_not_reask_same_field(
    tmp_path, monkeypatch
):
    loader, guard, module, context, data, config = setup(tmp_path)
    monkeypatch.setattr(module.core, "ensure_ir_ready", AsyncMock())
    executed = []

    @tool
    async def consumer() -> str:
        """An IR consumer whose side effects must wait for clarification."""
        executed.append("run")
        return "checked"

    consumer = consumer.model_copy(update={"name": "de-agent__check_and_repair_sql"})
    ask = loader.load(ROOT, "tools/hitl.py:request_human_feedback")
    prepare = loader.load(ROOT, "hooks/hitl.py:prepare_context")
    model = ScriptedModel(
        responses=[
            call(consumer.name, {}, "before"),
            call(consumer.name, {}, "after"),
            AIMessage(content="完成"),
        ]
    )
    graph = create_deep_agent(
        model=model,
        tools=[consumer, tool(ask).model_copy(update={"name": "de-agent__request_human_feedback"})],
        context_schema=SimpleNamespace,
        checkpointer=InMemorySaver(),
        middleware=[
            compile_hook(HookSpec(event=event, entrypoint="unused.py:handle"), handler, name=event)
            for event, handler in [("before_model", prepare), ("after_model", guard)]
        ],
    )
    run = {"configurable": {"thread_id": "ir-hitl"}}
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="统计用户")]}, run, context=context
    )
    assert result["__interrupt__"] and not executed
    assert "DataTaskIR" in result["__interrupt__"][0].value["pending_action"]
    result = await graph.ainvoke(Command(resume="用 user_id"), run, context=context)
    assert executed == ["run"]
    hitl = (await graph.aget_state(run)).values["hitl"]
    assert hitl["count"] == 1 and hitl["cleared_fields"] == ["identity"]
    assert data["data_task_ir"]["ir_hitl_best_effort"] is True


@pytest.mark.parametrize("reason", ["budget", "field", "fingerprint", "fires", "disabled"])
async def test_fuses_and_opt_out_release_consumers(tmp_path, monkeypatch, reason):
    _, guard, module, context, _, config = setup(tmp_path)
    monkeypatch.setattr(module.core, "ensure_ir_ready", AsyncMock())
    last = call("de-agent__check_and_repair_sql", {}, "consume")
    state = {"messages": [HumanMessage(content="q"), last]}
    if reason == "disabled":
        config["data_task_ir"]["enable_ir_hitl"] = False
    else:
        data = module.initial(context, state)
        if reason == "budget":
            data["count"] = data["budget"]
        if reason == "field":
            data["field_asks"]["identity"] = 2
        if reason == "fires":
            data["ir_guard_fires"] = 2
        if reason == "fingerprint":
            module.ir_questions(context, data)
            data["fingerprint_asks"][data["ir_fingerprint"]] = 2
        state["hitl"] = data
    update = await guard(state, SimpleNamespace(context=context), params={})
    assert not update.get("messages")


def test_ir_does_not_absorb_system_fallback_as_user_confirmation():
    hook = PythonLoader().load(ROOT, "hooks/ir.py:prepare_turn")
    fn = hook.__globals__["ir_session"].feedback_content
    message = ToolMessage(
        content="自行判断",
        name="de-agent__request_human_feedback",
        tool_call_id="ask",
        response_metadata={"hitl": {"source": "policy"}},
    )
    assert fn(message) == ""
    assert (
        fn(message.model_copy(update={"response_metadata": {"hitl": {"source": "human"}}}))
        == "自行判断"
    )


async def test_restart_restores_ir_evidence_boundary_and_absorbs_new_answer(ir_h):
    h = ir_h
    h.options["hitl"] = {"enabled": True}
    state = await h.turn("近7天下载次数")
    h.evidence()
    await h.consume(state)
    current = h.session.current(h.context)
    boundary = h.states["data_task_ir"]["turn"]
    state["hitl"] = {
        "task_key": current["turn_id"],
        "ir_turn": {
            "query": current["query"],
            "started_at_ns": boundary["started_at_ns"],
            "old_feedback_ids": list(boundary["old_feedback_ids"]),
            "turn_id": current["turn_id"],
            "task_id": current["snapshot"]["task_id"],
        },
    }
    answer = ToolMessage(
        content="使用 user_id",
        name="de-agent__request_human_feedback",
        tool_call_id="resume",
        response_metadata={"hitl": {"source": "human"}},
    )
    state["messages"].append(answer)
    fresh = {}
    h.context.get_plugin_state = lambda _: fresh
    prepare = h.loader.load(ROOT, "hooks/hitl.py:prepare_context")
    await prepare(state, SimpleNamespace(context=h.context), params={})
    restored = h.session.current(h.context)
    assert "resume" not in restored["old_feedback_ids"]
    assert restored["turn_id"] == current["turn_id"]
    await h.core.ensure_ir_ready(h.context, state)
    assert restored["snapshot"]["feedback"][-1]["content"] == "使用 user_id"
    assert restored["snapshot"]["task_id"] == current["snapshot"]["task_id"]
