"""One business-facing review before SQL generation, scoped to the IR task."""

from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.messages import AIMessage
from utils.data_task_ir import core
from utils.data_task_ir.render import format_ir_caliber_review_pending
from utils.data_task_ir.runtime import ir_enabled, ir_options, ir_state
from utils.feedback import record_feedback

from dataagent import HITLHandler, HITLState, InteractionRequest, ResumeDecision, TriggerDecision


def _merge(current, updates):
    return {**(current or {}), **updates}


class CaliberState(HITLState):
    ir_caliber_reviews: NotRequired[Annotated[dict[str, Any], PrivateStateAttr, _merge]]


class CaliberReview(HITLHandler):
    state_schema = CaliberState

    async def trigger_policy(self, context, *, params):
        host = context.runtime.context
        if not ir_enabled(host) or not ir_options(host).get("enable_ir_caliber_review", False):
            return TriggerDecision(triggered=False)
        messages = context.state.get("messages", [])
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage):
            return TriggerDecision(triggered=False)
        calls = last.tool_calls
        consumer = any(
            (c["name"] == "task" and c["args"].get("subagent_type") == "nl2sql")
            or c["name"] == "de-agent__check_and_repair_sql"
            for c in calls
        )
        if not consumer:
            return TriggerDecision(triggered=False)
        await core.ensure_ir_ready(host, context.state)
        cache = ir_state(host)
        snapshot = (cache.get("session") or {}).get("snapshot") or {}
        task = snapshot.get("task_id")
        if not task or cache.get("ir_fallback") or cache.get("ir_turn_suppressed"):
            return TriggerDecision(triggered=False)
        if context.state.get("ir_caliber_reviews", {}).get(task):
            return TriggerDecision(triggered=False)
        fields = cache.get("ir_field_values", [])
        # Unresolved questions are handled by the earlier IRQuestionGuard.
        if any(f.get("unresolved") for f in fields):
            return TriggerDecision(triggered=False)
        question = format_ir_caliber_review_pending(fields)
        return TriggerDecision(
            triggered=bool(question),
            reason="生成 SQL 前确认业务口径",
            payload={"task": task, "question": question},
        )

    def interaction_builder(self, context, *, params):
        return InteractionRequest(question=context.decision.payload["question"])

    def resume_handler(self, context, *, params):
        record_feedback(context.runtime.context, context.answer)
        return ResumeDecision(
            action="replan",
            message=f"用户口径反馈（优先遵从）：{context.answer}",
            state_update={"ir_caliber_reviews": {context.decision.payload["task"]: True}},
        )
