"""Legacy completion guards, exercised with native model nodes and checkpoints."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from dataagent.core.graph import create_deep_agent
from dataagent.extensions.hitl import HITLContext, HITLMiddleware
from dataagent.extensions.loading import PythonLoader

ROOT = Path(__file__).resolve().parents[1] / "dataagent/plugins/de-agent"


def handler(name):
    return PythonLoader().load(ROOT, f"hitl/completion_guards.py:{name}")()


def context(message, options=None, state=None):
    host = SimpleNamespace(get_plugin_config=lambda _: {"hitl": options or {}})
    return HITLContext(
        state or {"messages": [HumanMessage(content="SQL", id="user"), message]},
        SimpleNamespace(context=host),
        None,
        "de-agent/guard",
        event="after_model",
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("请选择源表。\n请确认使用哪个源表？", True),
        ("请您确认输出列清单。", True),
        ("您倾向 user_id 吗？", True),
        ("交付已完成。", False),
        ("说明中的问题？", False),
        ("", False),
    ],
)
def test_text_question_legacy_detection(text, expected):
    guard = handler("TextQuestionGuard")
    ctx = context(AIMessage(content=text))
    result = guard.trigger_policy(ctx, params={})
    assert result.triggered is expected


@pytest.mark.parametrize(
    "name,options",
    [
        ("TextQuestionGuard", {}),
        ("HumanFeedbackGuard", {}),
    ],
)
def test_tool_calls_never_trigger_completion_guards(name, options):
    assert not handler(name).trigger_policy(context(call("task", {}), options), params={}).triggered


@pytest.mark.parametrize(
    "name,options,cap",
    [
        ("TextQuestionGuard", {}, 2),
        ("HumanFeedbackGuard", {}, 1),
    ],
)
async def test_guard_caps_replay_and_new_user_turn(tmp_path, name, options, cap):
    spec = {
        "policies": [
            {
                "id": "guard",
                "at": "after_model",
                "handler": f"hitl/completion_guards.py:{name}",
                "params": options,
            }
        ]
    }
    config = {"configurable": {"thread_id": "guards"}}
    path = str(tmp_path / "checkpoint.sqlite")
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph = create_deep_agent(
            model=ScriptedModel(responses=[AIMessage(content="请确认使用哪个源表？")]),
            middleware=[HITLMiddleware.from_config(spec, root=ROOT, namespace="de-agent")],
            checkpointer=saver,
        )
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="SQL", id="first")]}, config
        )
        assert result["__interrupt__"]
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph = create_deep_agent(
            model=ScriptedModel(responses=[AIMessage(content="请确认使用哪个源表？")] * (cap + 2)),
            middleware=[HITLMiddleware.from_config(spec, root=ROOT, namespace="de-agent")],
            checkpointer=saver,
        )
        for _ in range(cap):
            result = await graph.ainvoke(
                Command(resume={result["__interrupt__"][0].id: "同意"}), config
            )
        assert not result.get("__interrupt__")
        state = (await graph.aget_state(config)).values
        assert state["completion_guards"]["de-agent/guard"]["count"] == cap
        result = await graph.ainvoke(
            {"messages": [HumanMessage(content="下一任务", id="second")]}, config
        )
        assert result["__interrupt__"]


def test_registered_final_confirmation_triggers_without_an_extra_flag_and_formats_conditions():
    guard = handler("HumanFeedbackGuard")
    ctx = context(AIMessage(content="done"))
    assert guard.trigger_policy(ctx, params={}).triggered
    request = guard.interaction_builder(ctx, params={"conditions": ["列数", "口径"]})
    assert "• 列数" in request.question and "• 口径" in request.question


@pytest.mark.parametrize(
    "enabled,include_policy,expected",
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
    ],
)
@pytest.mark.parametrize(
    "name,content",
    [
        ("HumanFeedbackGuard", "done"),
        ("TextQuestionGuard", "请确认使用哪个源表？"),
    ],
)
async def test_completion_guards_are_controlled_by_policy_registration(
    enabled, include_policy, expected, name, content
):
    from langgraph.checkpoint.memory import InMemorySaver

    policy = {"id": "guard", "at": "after_model", "handler": f"hitl/completion_guards.py:{name}"}
    mw = HITLMiddleware.from_config(
        {
            "enabled": enabled,
            "policies": [policy] if include_policy else [],
        },
        root=ROOT,
        namespace="de-agent",
    )
    graph = create_deep_agent(
        model=ScriptedModel(responses=[AIMessage(content=content)]),
        middleware=[mw],
        checkpointer=InMemorySaver(),
    )
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="SQL")]},
        {"configurable": {"thread_id": "policy-switch"}},
    )
    assert bool(result.get("__interrupt__")) is expected


def test_text_question_limit_remains_effective_without_a_separate_enable_flag():
    guard = handler("TextQuestionGuard")
    ctx = context(AIMessage(content="请确认使用哪个源表？"))
    assert guard.trigger_policy(ctx, params={}).triggered
    assert not guard.trigger_policy(ctx, params={"text_question_max_fires": 0}).triggered


def test_text_question_limit_is_not_read_from_business_config():
    guard = handler("TextQuestionGuard")
    ctx = context(AIMessage(content="请确认使用哪个源表？"), {"text_question_max_fires": 0})
    assert guard.trigger_policy(ctx, params={}).triggered
    assert not guard.trigger_policy(ctx, params={"text_question_max_fires": 0}).triggered
