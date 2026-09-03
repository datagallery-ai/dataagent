"""Native human-in-the-loop tool for LangGraph execution."""

from __future__ import annotations

from typing import Annotated, Any

from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import interrupt


@tool("ask_user")
def request_human_feedback(
    reason: str,
    pending_action: str = "",
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Any:
    """Pause execution and request the smallest piece of human input needed to continue.

    Use this when reliable progress needs clarification, confirmation, missing
    business context, or approval for a consequential action.

    Args:
        reason: Why human input is required now.
        pending_action: Concrete question or action the user is being asked to confirm.
        tool_call_id: Injected LangChain tool-call identifier used to correlate the AG-UI response.

    Returns:
        The value supplied when the LangGraph run is resumed.
    """
    question = pending_action.strip() or reason.strip()
    return interrupt(
        {
            "type": "agent_interrupt",
            "toolCallId": tool_call_id,
            "toolName": "ask_user",
            "suspendPayload": {"question": question, "reason": reason},
            "args": {"question": question},
            "reason": reason,
        }
    )
