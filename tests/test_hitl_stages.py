"""Shared interaction lifecycle across all native middleware stages."""

from dataclasses import replace
from typing import get_args

import pytest
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from test_hitl import build, resume

from dataagent.core.graph import create_deep_agent
from dataagent.extensions.hitl import (
    HITLHandler,
    HITLMiddleware,
    HITLPolicy,
    HITLPolicySpec,
    HITLStage,
    ResumeDecision,
    TriggerDecision,
)

STAGES = get_args(HITLStage)


class ReviewOnce(HITLHandler):
    def __init__(self, action="continue"):
        self.action = action

    def trigger_policy(self, context, *, params):
        return TriggerDecision(triggered=not context.state.get("ir", {}).get("reviewed"))

    def resume_handler(self, context, *, params):
        return ResumeDecision(
            action=self.action,
            message=f"Review {context.event}: {context.answer}",
            state_update={"ir": {"reviewed": True}},
        )


def middleware(stage, handler=None):
    return HITLMiddleware(
        [
            HITLPolicy(
                HITLPolicySpec(id="review", at=stage),
                handler or ReviewOnce(),
            )
        ]
    )


def assert_tool_pairs(messages):
    unresolved = set()
    for message in messages:
        if isinstance(message, AIMessage):
            assert not unresolved, "A new model response preceded tool completion"
            unresolved.update(c["id"] for c in message.tool_calls)
        elif isinstance(message, ToolMessage):
            assert message.tool_call_id in unresolved
            unresolved.remove(message.tool_call_id)
    assert not unresolved


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("action", ["continue", "replan", "reject"])
async def test_stage_timing_actions_and_state_updates(stage, action):
    executed = []
    graph = build(
        middleware(stage, ReviewOnce(action)),
        InMemorySaver(),
        executed,
        responses=[
            call("consume", {}, "original"),
            AIMessage(content="done"),
            AIMessage(content="revised"),
        ],
    )
    config = {"configurable": {"thread_id": "stages"}}
    result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    event = result["__interrupt__"][0].value
    assert event["at"] == stage
    assert event["tool_call_id"] == ("original" if stage.endswith("tool") else None)
    assert len(executed) == (1 if stage in {"after_tool", "after_agent"} else 0)
    result = await graph.ainvoke(resume(result, "confirmed"), config)
    assert not result.get("__interrupt__")
    assert result["ir"] == {"reviewed": True}
    should_execute = (
        stage in {"after_tool", "after_agent"}
        or action == "continue"
        or (action == "replan" and stage in {"before_agent", "before_model"})
    )
    assert len(executed) == int(should_execute)
    if action == "reject" and not stage.endswith("tool"):
        assert "本次执行已被拒绝" in result["messages"][-1].content
    if stage == "after_agent" and action == "replan":
        assert result["messages"][-1].content == "revised"
    history = (await graph.aget_state(config)).values["hitl_history"]
    assert len(history) == 1
    assert next(iter(history.values()))["at"] == stage
    assert_tool_pairs(result["messages"])


async def test_all_stages_follow_native_lifecycle_order():
    class Continue(HITLHandler):
        def resume_handler(self, context, *, params):
            return ResumeDecision(action="continue")

    mw = HITLMiddleware(
        [HITLPolicy(HITLPolicySpec(id=stage, at=stage), Continue()) for stage in STAGES]
    )
    graph = build(mw, InMemorySaver(), [])
    config = {"configurable": {"thread_id": "order"}}
    result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    events = []
    while result.get("__interrupt__"):
        events.append(result["__interrupt__"][0].value["at"])
        assert len(events) <= 8
        result = await graph.ainvoke(resume(result, "yes"), config)
    assert events == [
        "before_agent",
        "before_model",
        "after_model",
        "before_tool",
        "after_tool",
        "before_model",
        "after_model",
        "after_agent",
    ]
    assert len((await graph.aget_state(config)).values["hitl_history"]) == 8


@pytest.mark.parametrize("stage", STAGES)
async def test_sqlite_rebuild_does_not_repeat_preparation_or_completed_tool(tmp_path, stage):
    class Rebuilt(ReviewOnce):
        def trigger_policy(self, context, *, params):
            # State-stage nodes may be revisited after the first completed resume.
            if context.state.get("ir", {}).get("reviewed"):
                return TriggerDecision(triggered=False)
            raise AssertionError("Preparation was not reused")

        def interaction_builder(self, context, *, params):
            raise AssertionError("Question was regenerated")

    path = str(tmp_path / "stages.sqlite")
    config = {"configurable": {"thread_id": "sqlite"}}
    executed = []
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph = build(middleware(stage), saver, executed)
        result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        responses = (
            [call("consume", {}, "original")] if stage in {"before_agent", "before_model"} else []
        )
        responses.append(AIMessage(content="done"))
        graph = build(middleware(stage, Rebuilt()), saver, executed, responses=responses)
        result = await graph.ainvoke(resume(result, "yes"), config)
        assert not result.get("__interrupt__") and len(executed) == 1


async def test_after_model_replan_closes_all_calls_without_executing_any():
    message = AIMessage(
        content="", tool_calls=[call("consume", {}, key).tool_calls[0] for key in ("a", "b")]
    )
    executed = []
    graph = build(
        middleware("after_model", ReviewOnce("replan")),
        InMemorySaver(),
        executed,
        responses=[message, AIMessage(content="replanned")],
    )
    config = {"configurable": {"thread_id": "batch"}}
    result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    result = await graph.ainvoke(resume(result, "change plan"), config)
    assert not executed and result["messages"][-1].content == "replanned"
    assert_tool_pairs(result["messages"])


async def test_after_tool_context_and_command_result_are_preserved():
    from langchain.tools import ToolRuntime
    from langgraph.types import Command
    from test_hitl import IRState

    class Inspect(ReviewOnce):
        def trigger_policy(self, context, *, params):
            assert context.state["ir"] == {"from_tool": True}
            assert isinstance(context.tool_result, Command)
            assert context.tool_result.update["messages"][0].content == "tool result"
            return TriggerDecision()

    @tool
    def consume(runtime: ToolRuntime) -> Command:
        """Return business state and a tool message."""
        return Command(
            update={
                "ir": {"from_tool": True},
                "messages": [ToolMessage(content="tool result", tool_call_id=runtime.tool_call_id)],
            }
        )

    graph = create_deep_agent(
        model=ScriptedModel(responses=[call("consume", {}, "cmd"), AIMessage(content="done")]),
        tools=[consume],
        middleware=[middleware("after_tool", Inspect("replan"))],
        state_schema=IRState,
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "command"}}
    result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    result = await graph.ainvoke(resume(result, "revise"), config)
    assert result["ir"] == {"reviewed": True}
    assert any(
        isinstance(m, ToolMessage) and m.content == "tool result" for m in result["messages"]
    )
    assert_tool_pairs(result["messages"])


async def test_changing_pending_stage_fails_closed():
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "changed"}}
    mw = middleware("before_model")
    graph = build(mw, saver, [])
    result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    policy = next(iter(mw.policies.values()))
    changed = replace(policy, spec=policy.spec.model_copy(update={"at": "after_model"}))
    graph = build(HITLMiddleware([changed]), saver, [], responses=[])
    with pytest.raises(ValueError, match="incompatible version"):
        await graph.ainvoke(resume(result, "yes"), config)


@pytest.mark.parametrize("stage", ["before_tool", "after_tool"])
@pytest.mark.parametrize("action", ["continue", "replan", "reject"])
async def test_return_direct_tool_obeys_resume_decision(stage, action):
    from test_hitl import IRState

    executed = []

    @tool(return_direct=True)
    def consume() -> str:
        """Return directly unless HITL requires another model turn."""
        executed.append(True)
        return "direct result"

    model = ScriptedModel(responses=[call("consume", {}, "direct"), AIMessage(content="revised")])
    graph = create_deep_agent(
        model=model,
        tools=[consume],
        middleware=[middleware(stage, ReviewOnce(action))],
        state_schema=IRState,
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "direct"}}
    result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    result = await graph.ainvoke(resume(result, "yes"), config)
    assert len(executed) == int(stage == "after_tool" or action == "continue")
    assert model.cursor == (1 if action == "continue" else 2)
    if action != "continue":
        assert result["messages"][-1].content == "revised"


async def test_parallel_after_tool_reviews_reuse_completed_execution():
    class Continue(HITLHandler):
        def resume_handler(self, context, *, params):
            return ResumeDecision(action="continue")

    message = AIMessage(
        content="", tool_calls=[call("consume", {}, key).tool_calls[0] for key in ("a", "b")]
    )
    saver, executed = InMemorySaver(), []
    config = {"configurable": {"thread_id": "parallel-after"}}
    graph = build(middleware("after_tool", Continue()), saver, executed, responses=[message])
    result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    assert len(executed) == 2 and len(result["__interrupt__"]) == 2
    graph = build(middleware("after_tool", Continue()), saver, executed, responses=[])
    result = await graph.ainvoke(resume(result, "yes"), config)
    assert len(executed) == 2 and len(result["__interrupt__"]) == 1
    graph = build(
        middleware("after_tool", Continue()), saver, executed, responses=[AIMessage(content="done")]
    )
    result = await graph.ainvoke(resume(result, "yes"), config)
    assert len(executed) == 2
    assert len((await graph.aget_state(config)).values["hitl_history"]) == 2


async def test_mixed_return_direct_batch_does_not_schedule_extra_model_turn():
    from test_hitl import IRState

    @tool(return_direct=True)
    def direct() -> str:
        """A direct-return tool."""
        return "direct"

    @tool
    def normal() -> str:
        """A normal tool in the same batch."""
        return "normal"

    message = AIMessage(
        content="",
        tool_calls=[
            call("direct", {}, "direct").tool_calls[0],
            call("normal", {}, "normal").tool_calls[0],
        ],
    )
    mw = HITLMiddleware(
        [
            HITLPolicy(
                HITLPolicySpec(id="direct_review", at="after_tool", matcher="direct"),
                ReviewOnce("replan"),
            )
        ]
    )
    model = ScriptedModel(responses=[message, AIMessage(content="done")])
    graph = create_deep_agent(
        model=model,
        tools=[direct, normal],
        middleware=[mw],
        state_schema=IRState,
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "mixed"}}
    result = await graph.ainvoke({"messages": [("user", "SQL")], "ir": {}}, config)
    result = await graph.ainvoke(resume(result, "revise"), config)
    assert model.cursor == 2 and result["messages"][-1].content == "done"
    assert not any((await graph.aget_state(config)).values["hitl_replans"].values())
