"""Completion guards migrated from planner post-hooks to after_model HITL."""

import hashlib
import re
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.messages import AIMessage, HumanMessage

from dataagent import (
    HITLHandler,
    HITLState,
    InteractionRequest,
    ResumeDecision,
    TriggerDecision,
)

_AWAITING = re.compile(
    r"(请问|请确认|请您|您确认|您倾向|您选择|您决定|是否同意|是否确认|是否需要|"
    r"确认一下|帮我确认|您能|告诉我|您指定|吗\s*[？?]|呢\s*[？?])"
)


def _merge(current, updates):
    return {**(current or {}), **updates}


class CompletionGuardState(HITLState):
    completion_guards: NotRequired[Annotated[dict[str, Any], PrivateStateAttr, _merge]]


def _users(messages):
    # Middleware feedback is synthetic HumanMessage, not a new user turn.
    return [
        m
        for m in messages
        if isinstance(m, HumanMessage) and not str(m.id or "").startswith("hitl_feedback_")
    ]


def _scope(messages):
    users = _users(messages)
    if not users:
        return "no-user"
    last = users[-1]
    return hashlib.sha256(f"{len(users)}:{last.id}:{last.content}".encode()).hexdigest()


class _CompletionGuard(HITLHandler):
    state_schema = CompletionGuardState

    def _prepare(self, context):
        messages = context.state.get("messages", [])
        last = messages[-1] if messages else None
        if context.event != "after_model" or not isinstance(last, AIMessage):
            return None
        if last.tool_calls or last.invalid_tool_calls:
            return None
        scope = _scope(messages)
        saved = context.state.get("completion_guards", {}).get(context.policy_id, {})
        count = saved.get("count", 0) if saved.get("scope") == scope else 0
        return last, scope, count

    def resume_handler(self, context, *, params):
        payload = context.decision.payload
        return ResumeDecision(
            action="replan",
            message=f"用户反馈：{context.answer}\n请据此调整或完成交付，不要重复询问已回答的问题。",
            state_update={
                "completion_guards": {
                    context.policy_id: {
                        "scope": payload["scope"],
                        "count": payload["count"] + 1,
                        "answer": context.answer,
                        "query": str(_users(context.state["messages"])[-1].content)
                        if _users(context.state["messages"])
                        else "",
                        "reply_id": f"{context.policy_id}:{payload['scope']}:{payload['count'] + 1}",
                    }
                }
            },
        )


class TextQuestionGuard(_CompletionGuard):
    def trigger_policy(self, context, *, params):
        prepared = self._prepare(context)
        if prepared is None:
            return TriggerDecision(triggered=False)
        last, scope, count = prepared
        limit = params.get("text_question_max_fires", 2)
        if type(limit) is not int or limit < 0:
            raise ValueError("text_question_max_fires must be a nonnegative integer")
        if count >= limit:
            return TriggerDecision(triggered=False)
        content = last.content if isinstance(last.content, str) else ""
        lines = [line for line in content.strip().splitlines() if line.strip()]
        tail = lines[-1].rstrip() if lines else ""
        awaiting = _AWAITING.search(tail) and (
            tail.endswith(("？", "?", "吗", "呢"))
            or any(word in tail for word in ("请问", "请确认", "请您"))
        )
        return TriggerDecision(
            triggered=bool(awaiting),
            reason="普通回复正在等待用户答复，转入正式交互",
            payload={"scope": scope, "count": count, "question": content.strip()[:2000]},
        )

    def interaction_builder(self, context, *, params):
        return InteractionRequest(question=context.decision.payload["question"])


class HumanFeedbackGuard(_CompletionGuard):
    def trigger_policy(self, context, *, params):
        prepared = self._prepare(context)
        if prepared is None:
            return TriggerDecision(triggered=False)
        _, scope, count = prepared
        return TriggerDecision(
            triggered=count == 0,
            reason="任务准备结束，需要最终确认",
            payload={"scope": scope, "count": count},
        )

    def interaction_builder(self, context, *, params):
        conditions = params.get("conditions")
        if isinstance(conditions, list):
            conditions = "\n".join(
                f"• {str(item).strip()}" for item in conditions if str(item).strip()
            )
        if conditions is not None and not isinstance(conditions, str):
            raise ValueError("Final feedback conditions must be a string or list")
        question = "任务已完成，请确认最终结果是否满足需求。如需调整，请说明需要修改的部分。"
        if conditions and conditions.strip():
            question += "\n" + conditions.strip()
        return InteractionRequest(question=question)
