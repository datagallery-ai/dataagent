"""A native clarification tool. Interrupts resume the same checkpointed tool call."""

from langchain.tools import ToolRuntime
from langgraph.types import Command, interrupt
from utils.hitl import answer_update, auto_answer, enabled, history_reply, initial, options

from dataagent.models import ModelContext


async def request_human_feedback(
    reason: str,
    pending_action: str,
    runtime: ToolRuntime[ModelContext],
) -> Command:
    """Ask one blocking business question before continuing.

    Args:
        reason: Why reliable progress needs clarification. Verify metadata first.
        pending_action: Concrete preferred proposal and 1–2 alternatives, with actual
            table/field names, time windows or output counts. Never just ask 'confirm?'.
            Quote prior confirmations; do not ask again about an answered point.
    """
    context = runtime.context
    if not enabled(context):
        raise ValueError("HITL is disabled")
    if not reason.strip() or not pending_action.strip():
        raise ValueError("reason and pending_action must contain a concrete question")
    data = initial(context, runtime.state)
    question = reason.strip() + "\n" + pending_action.strip()
    answer = history_reply(data, question)
    source, action = "policy", "DEDUP_HIT"
    if data["count"] >= data["budget"]:
        answer, action = (
            "已达交互预算上限，请基于已有信息继续，明确列出未确认的假设，不再询问。",
            "BUDGET_EXHAUSTED",
        )
    if answer is None and options(context).get("auto_resolve", False) is True:
        source = "evaluation"
        try:
            answer, action = await auto_answer(
                context, runtime.state, data, question, runtime.tool_call_id
            )
        except (TimeoutError, OSError, ValueError):
            answer, action = "自动应答未完成，请基于已确认信息继续，并说明未决项。", "FALLBACK"
    if action == "FALLBACK":
        source = "policy"
    if answer is None:
        # LangGraph re-executes this call on resume. No side effects precede interrupt.
        answer = interrupt(
            {
                "type": "human_feedback",
                "reason": reason,
                "pending_action": pending_action,
                "message": question,
                "tool_call_id": runtime.tool_call_id,
                "remaining": data["budget"] - data["count"],
            }
        )
        if isinstance(answer, dict) and answer.get("__agui_cancelled__"):
            answer, source, action = "本次澄清已取消，不视为用户确认。", "policy", "CANCELLED"
        else:
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("Human feedback must be nonempty text")
            answer, source, action = answer.strip(), "human", "HUMAN"
    update = answer_update(
        runtime.state, data, question, answer, runtime.tool_call_id, source=source, action=action
    )
    return Command(update=update or {})
