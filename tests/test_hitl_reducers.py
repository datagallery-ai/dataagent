"""Previews and commits must apply the same ordered native channel updates."""

import operator
from typing import Annotated

import pytest
from conftest import ScriptedModel, call
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from dataagent.core.graph import create_deep_agent
from dataagent.extensions.hitl import (
    HITLHandler,
    HITLMiddleware,
    HITLPolicy,
    HITLPolicySpec,
    HITLState,
    ResumeDecision,
)


def digits(left, right):
    return left * 10 + right


@pytest.mark.parametrize("stage", ["before_tool", "before_model"])
async def test_nonassociative_reducer_preserves_each_delta_and_preview(stage):
    class State(HITLState):
        number: Annotated[int, digits]

    seen = []
    tool_seen = []

    class Handler(HITLHandler):
        def resume_handler(self, context, *, params):
            seen.append(context.state["number"])
            return ResumeDecision(action="continue", state_update={"number": params["delta"]})

    @tool
    def consume(runtime: ToolRuntime) -> Command:
        """Read the approved state and produce another reducer delta."""
        tool_seen.append(runtime.state["number"])
        return Command(
            update={
                "number": 4,
                "messages": [ToolMessage(content="done", tool_call_id=runtime.tool_call_id)],
            }
        )

    policies = [
        HITLPolicy(HITLPolicySpec(id=f"gate{i}", at=stage, params={"delta": i}), Handler())
        for i in (2, 3)
    ]
    policies.append(
        HITLPolicy(HITLPolicySpec(id="after", at="after_tool", params={"delta": 5}), Handler())
    )
    # before_model gates each call; gate only the initial model invocation.
    if stage == "before_model":
        from dataagent.extensions.hitl import TriggerDecision

        Handler.trigger_policy = lambda self, context, *, params: TriggerDecision(
            triggered=context.event == "after_tool" or context.state["number"] == 1
        )
    graph = create_deep_agent(
        model=ScriptedModel(responses=[call("consume", {}), AIMessage(content="done")]),
        tools=[consume],
        middleware=[HITLMiddleware(policies)],
        state_schema=State,
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "reducer"}}
    result = await graph.ainvoke({"messages": [("user", "go")], "number": 1}, config)
    for _ in (2, 3, 5):
        result = await graph.ainvoke(Command(resume={result["__interrupt__"][0].id: "yes"}), config)
    assert 12 in seen and 1234 in seen
    assert tool_seen == [123]
    assert result["number"] == 12345


async def test_parallel_tools_keep_additive_deltas_without_overwriting_siblings():
    class State(HITLState):
        items: Annotated[list[str], operator.add]

    seen = []

    class Handler(HITLHandler):
        def resume_handler(self, context, *, params):
            return ResumeDecision(action="continue", state_update={"items": ["approved"]})

    @tool
    def consume(runtime: ToolRuntime) -> Command:
        """Record preview and contribute one more item."""
        seen.append(runtime.state["items"])
        return Command(
            update={
                "items": ["tool"],
                "messages": [ToolMessage(content="ok", tool_call_id=runtime.tool_call_id)],
            }
        )

    message = AIMessage(
        content="", tool_calls=[call("consume", {}, key).tool_calls[0] for key in ("a", "b")]
    )
    graph = create_deep_agent(
        model=ScriptedModel(responses=[message, AIMessage(content="done")]),
        tools=[consume],
        middleware=[HITLMiddleware([HITLPolicy(HITLPolicySpec(id="gate"), Handler())])],
        state_schema=State,
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "parallel-reducer"}}
    result = await graph.ainvoke({"messages": [("user", "go")], "items": ["existing"]}, config)
    result = await graph.ainvoke(
        Command(resume={i.id: "yes" for i in result["__interrupt__"]}), config
    )
    assert seen == [["existing", "approved"], ["existing", "approved"]]
    assert result["items"] == ["existing", "approved", "tool", "approved", "tool"]
