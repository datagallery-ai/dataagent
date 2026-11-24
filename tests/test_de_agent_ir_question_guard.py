"""Real plugin handler on native HITL, with offline IR extraction fixtures."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from dataagent.config.strict_yaml import read_yaml
from dataagent.core.graph import create_deep_agent
from dataagent.extensions.hitl import HITLContext, HITLMiddleware
from dataagent.extensions.loading import PythonLoader

ROOT = Path(__file__).resolve().parents[1] / "dataagent/plugins/de-agent"


@pytest.fixture
def domain(monkeypatch):
    cls = PythonLoader().load(ROOT, "hitl/ir_question_guard.py:IRQuestionGuard")
    module = cls.trigger_policy.__globals__
    options = {"enabled": True}
    cache = {
        "turn": {"query": "SQL"},
        "session": {"query": "SQL", "snapshot": {"task_id": "task1", "queries": ["SQL"]}},
        "ir_field_values": [
            {
                "field_id": "identity",
                "unresolved": ["identifier"],
                "question": "user_id 还是 device_id？",
            }
        ],
    }
    host = SimpleNamespace(
        get_plugin_config=lambda _: {"data_task_ir": options},
        get_plugin_state=lambda _: {"data_task_ir": cache},
    )
    refreshes = []

    async def refresh(context, state):
        refreshes.append(deepcopy(state.get("ir_clarification", {})))

    monkeypatch.setattr(module["core"], "ensure_ir_ready", refresh)
    return cls, module, options, cache, host, refreshes


def ctx(host, *, state=None, event="before_tool", subagent="nl2sql"):
    return HITLContext(
        state or {"messages": [AIMessage(content="done")]},
        SimpleNamespace(context=host),
        {"name": "task", "id": "sql", "args": {"subagent_type": subagent}}
        if event == "before_tool"
        else None,
        "de-agent/ir",
        event=event,
    )


async def test_real_task_replans_then_releases_unresolved_answered_field(domain):
    cls, module, _, cache, host, refreshes = domain
    seen = []

    async def child(state):
        seen.append(state)
        return {"messages": [AIMessage(content="SQL")]}

    mw = HITLMiddleware.from_config(
        read_yaml(ROOT / ".plugin.yaml")["hitl"], root=ROOT, namespace="de-agent"
    )
    mw.policies = {
        key: replace(policy, handler=cls()) if policy.spec.id == "ir_question_guard" else policy
        for key, policy in mw.policies.items()
    }
    graph = create_deep_agent(
        model=ScriptedModel(
            responses=[
                call("task", {"subagent_type": "nl2sql", "description": "SQL"}, "sql1"),
                call("task", {"subagent_type": "nl2sql", "description": "SQL"}, "sql2"),
                AIMessage(content="done"),
                AIMessage(content="confirmed"),
            ]
        ),
        middleware=[mw],
        state_schema=module["IRQuestionState"],
        subagents=[{"name": "nl2sql", "description": "SQL", "runnable": RunnableLambda(child)}],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "ir"}}
    result = await graph.ainvoke({"messages": [("user", "SQL")]}, config, context=host)
    assert not seen and result["__interrupt__"]
    result = await graph.ainvoke(
        Command(resume={result["__interrupt__"][0].id: "user_id"}), config, context=host
    )
    assert len(seen) == 1
    assert result["__interrupt__"][0].value["policy_id"] == "de-agent/human_feedback_guard"
    result = await graph.ainvoke(
        Command(resume={result["__interrupt__"][0].id: "结果符合需求"}), config, context=host
    )
    assert not result.get("__interrupt__") and len(seen) == 1
    saved = (await graph.aget_state(config)).values["ir_clarification"]
    assert saved["answers"][0]["content"] == "user_id" and saved["turns"] == 1
    assert "ir_clarification" not in seen[0]  # private business state stays in parent
    assert cache["ir_field_values"][0]["unresolved"]  # reply never auto-confirms IR
    assert cache["ir_hitl_best_effort"]
    assert any(r.get("answers") for r in refreshes)


@pytest.mark.parametrize(
    "setting",
    [
        "disabled",
        "no_question",
        "nonblocking",
        "confirmed",
        "other_agent",
        "budget",
        "fires",
        "field",
        "fingerprint",
        "fingerprint_limit",
    ],
)
async def test_trigger_filters_and_caps(domain, setting):
    cls, _, options, cache, host, _ = domain
    state, params = {}, {}
    handler = cls()
    initial = await handler.trigger_policy(ctx(host), params={})
    refresh_count = len(domain[-1])
    if setting == "disabled":
        options["enabled"] = False
    if setting == "no_question":
        cache["ir_field_values"][0]["question"] = " "
    if setting == "nonblocking":
        params["hitl_blocking_fields"] = ["sources"]
    if setting == "confirmed":
        cache["ir_field_values"][0]["unresolved"] = []
    if setting == "budget":
        params["budget"] = 0
    if setting == "fires":
        params["hitl_question_guard_max_fires"] = 0
    if setting == "field":
        params["hitl_max_same_field_asks"] = 0
    if setting == "fingerprint_limit":
        params["hitl_max_same_fingerprint_asks"] = 0
    if setting == "fingerprint":
        state["ir_clarification"] = {
            "task_id": "task1",
            "turns": 0,
            "fields": {},
            "answers": [],
            "fingerprints": {initial.payload["fingerprint"]: 2},
        }
    decision = await handler.trigger_policy(
        ctx(
            host,
            state=state,
            subagent=("metadata-recall" if setting == "other_agent" else "nl2sql"),
        ),
        params=params,
    )
    assert not decision.triggered
    if setting == "disabled":
        assert len(domain[-1]) == refresh_count, "disabled IR must not run extraction"


async def test_ir_guard_uses_policy_registration_instead_of_the_legacy_enable_flag(domain):
    cls, _, options, _, host, _ = domain
    options["enable_ir_hitl"] = False
    assert (await cls().trigger_policy(ctx(host), params={})).triggered


async def test_ir_hitl_settings_are_not_read_from_business_config(domain):
    cls, _, options, _, host, _ = domain
    options.update(
        {
            "hitl_blocking_fields": [],
            "hitl_question_guard_max_fires": 0,
            "hitl_max_same_field_asks": 0,
            "hitl_max_same_fingerprint_asks": 0,
        }
    )
    assert (await cls().trigger_policy(ctx(host), params={})).triggered
    assert not (
        await cls().trigger_policy(ctx(host), params={"hitl_blocking_fields": []})
    ).triggered


@pytest.mark.parametrize(
    "enabled,include_policy,expected",
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
    ],
)
async def test_ir_guard_is_controlled_by_policy_registration(
    domain, enabled, include_policy, expected
):
    cls, _, _, _, host, _ = domain
    policy = {
        "id": "ir_question_guard",
        "at": "after_model",
        "handler": "hitl/ir_question_guard.py:IRQuestionGuard",
    }
    mw = HITLMiddleware.from_config(
        {
            "enabled": enabled,
            "policies": [policy] if include_policy else [],
        },
        root=ROOT,
        namespace="de-agent",
    )
    # Use the handler whose IR service was replaced by this test's offline fixture.
    mw.policies = {key: replace(value, handler=cls()) for key, value in mw.policies.items()}
    graph = create_deep_agent(
        model=ScriptedModel(responses=[AIMessage(content="done")]),
        middleware=[mw],
        checkpointer=InMemorySaver(),
    )
    result = await graph.ainvoke(
        {"messages": [("user", "SQL")]}, {"configurable": {"thread_id": "ir-policy"}}, context=host
    )
    assert bool(result.get("__interrupt__")) is expected


async def test_completion_guard_and_task_scoping(domain):
    cls, _, _, _, host, _ = domain
    state = {
        "messages": [AIMessage(content="done")],
        "ir_clarification": {
            "task_id": "old-task",
            "turns": 100,
        },
    }
    assert (
        await cls().trigger_policy(ctx(host, state=state, event="after_model"), params={})
    ).triggered
    state["messages"] = [call("task", {"subagent_type": "metadata-recall"})]
    assert not (
        await cls().trigger_policy(ctx(host, state=state, event="after_model"), params={})
    ).triggered


async def test_feedback_storage_is_idempotent_and_task_scoped(domain, monkeypatch):
    _, module, _, cache, host, _ = domain
    session_module = module["core"].ir_session
    session = cache["session"]
    session.update(feedback=[], turn_id="turn", old_feedback_ids=set())
    writes = []
    monkeypatch.setattr(session_module, "save", lambda context, snapshot: writes.append(snapshot))
    state = {
        "ir_clarification": {
            "task_id": "task1",
            "answers": [
                {"id": "stable", "content": "user_id", "query_index": 1},
            ],
        }
    }
    session_module.collect_feedback(host, state)
    session["turn_id"] = "rebuilt-turn"
    session_module.collect_feedback(host, state)
    assert len(writes) == len(session["feedback"]) == 1
    state["ir_clarification"]["task_id"] = "other"
    session_module.collect_feedback(host, state)
    assert len(writes) == 1


def test_completion_guard_reply_is_collected_once_as_raw_feedback(domain, monkeypatch):
    _, module, _, cache, host, _ = domain
    session_module = module["core"].ir_session
    session = cache["session"]
    session.update(feedback=[], turn_id="turn", old_feedback_ids=set())
    writes = []
    monkeypatch.setattr(session_module, "save", lambda context, snapshot: writes.append(snapshot))
    state = {
        "completion_guards": {
            "guard": {
                "query": "SQL",
                "reply_id": "reply1",
                "answer": "仅统计 user_id",
            }
        }
    }
    session_module.collect_feedback(host, state)
    session_module.collect_feedback(host, state)
    assert len(writes) == 1
    assert session["feedback"][0]["content"] == "仅统计 user_id"
