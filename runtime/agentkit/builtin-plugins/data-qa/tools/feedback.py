"""Human confirmation using LangGraph's checkpoint/resume protocol."""

from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from langgraph.types import interrupt

from dataagent.models import ModelContext


@tool
def request_human_feedback(question: str, runtime: ToolRuntime[ModelContext]) -> str:
    """Ask the user a clarification or confirmation question and wait for an answer."""
    return interrupt(
        {"kind": "human_feedback", "question": question, "tool_call_id": runtime.tool_call_id}
    )
