from __future__ import annotations

import json
from typing import Any

import pytest
from ag_ui.core.types import RunAgentInput
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

from datafoundry_api.agent import ScopedLangGraphAgent


class FeedbackState(MessagesState):
    """State used to verify the transport-level interrupt and resume contract."""

    answer: str


def _run_input(run_id: str, forwarded_props: dict[str, Any] | None = None) -> RunAgentInput:
    return RunAgentInput.model_validate(
        {
            "threadId": "feedback-thread",
            "runId": run_id,
            "messages": [{"id": "message-1", "role": "user", "content": "continue"}],
            "tools": [],
            "context": [],
            "forwardedProps": forwarded_props or {},
        }
    )


@pytest.mark.asyncio
async def test_legacy_agui_interrupt_and_resume_contract() -> None:
    saver = InMemorySaver()
    builder = StateGraph(FeedbackState)

    async def ask_for_feedback(_state: FeedbackState) -> dict[str, Any]:
        answer = interrupt(
            {
                "type": "agent_interrupt",
                "toolCallId": "call-1",
                "toolName": "ask_user",
                "suspendPayload": {"question": "Continue?"},
                "args": {"question": "Continue?"},
                "reason": "approval",
            }
        )
        return {"answer": str(answer), "messages": [AIMessage(content=f"answer:{answer}")]}

    builder.add_node("ask_for_feedback", ask_for_feedback)
    builder.add_edge(START, "ask_for_feedback")
    builder.add_edge("ask_for_feedback", END)
    graph = builder.compile(checkpointer=saver)
    agent = ScopedLangGraphAgent(user_id="user-a", name="test", graph=graph)

    first_events = [event async for event in agent.run(_run_input("run-1"))]
    interrupt_events = [event for event in first_events if getattr(event, "name", None) == "on_interrupt"]
    assert len(interrupt_events) == 1
    interrupt_value = json.loads(interrupt_events[0].value)
    assert interrupt_value.get("toolCallId") == "call-1"
    assert interrupt_value.get("toolName") == "ask_user"

    resumed_agent = ScopedLangGraphAgent(user_id="user-a", name="test", graph=graph)
    resumed_events = [
        event async for event in resumed_agent.run(_run_input("run-2", {"command": {"resume": "approved"}}))
    ]
    resumed_types = {getattr(getattr(event, "type", None), "value", None) for event in resumed_events}
    assert "RUN_FINISHED" in resumed_types
    assert not any(getattr(event, "name", None) == "on_interrupt" for event in resumed_events)

    state = await graph.aget_state({"configurable": {"thread_id": "datafoundry:user:user-a:thread:feedback-thread"}})
    assert state.values.get("answer") == "approved"
