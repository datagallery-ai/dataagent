"""Legacy IR-question guard as a subclassable HITL policy.

IR extraction owns field confirmation. This handler only records explicit replies
and bounded asking state; it never rewrites model messages or confirms guessed IR.
"""

import hashlib
import json
from copy import deepcopy
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.messages import AIMessage
from utils.data_task_ir import core
from utils.data_task_ir.runtime import ir_enabled, ir_state

from dataagent import (
    HITLHandler,
    HITLState,
    InteractionRequest,
    ResumeDecision,
    TriggerDecision,
)


class IRQuestionState(HITLState):
    ir_clarification: NotRequired[Annotated[dict[str, Any], PrivateStateAttr]]


def pending_questions(fields, blocking_fields):
    return [
        {
            "field_id": f["field_id"],
            "title": f.get("title") or f["field_id"],
            "question": f["question"].strip(),
            "unresolved": f["unresolved"],
        }
        for f in fields
        if f.get("field_id") in blocking_fields
        and f.get("unresolved")
        and isinstance(f.get("question"), str)
        and f["question"].strip()
    ]


def asking_state(state, task_id):
    current = state.get("ir_clarification", {})
    return (
        deepcopy(current)
        if current.get("task_id") == task_id
        else {
            "task_id": task_id,
            "turns": 0,
            "fingerprints": {},
            "fields": {},
            "answers": [],
        }
    )


def limits(params):
    result = {
        "budget": params.get("budget", 8),
        "fingerprint": params.get("hitl_max_same_fingerprint_asks", 2),
        "field": params.get("hitl_max_same_field_asks", 2),
        "fires": params.get("hitl_question_guard_max_fires", 2),
    }
    if any(type(value) is not int or value < 0 for value in result.values()):
        raise ValueError("IR HITL limits must be nonnegative integers")
    return result


class IRQuestionGuard(HITLHandler):
    state_schema = IRQuestionState

    async def trigger_policy(self, context, *, params):
        host = context.runtime.context
        if not ir_enabled(host):
            return TriggerDecision(triggered=False)
        call = context.tool_call

        def consumer(call):
            return (
                call["name"] == "task" and call["args"].get("subagent_type") == "nl2sql"
            ) or call["name"] in params.get("consumerTools", ["de-agent__check_and_repair_sql"])

        if context.event == "before_tool":
            if not (call and consumer(call)):
                return TriggerDecision(triggered=False)
        elif context.event == "after_model":
            last = context.state["messages"][-1]
            if not isinstance(last, AIMessage) or (
                last.tool_calls and not any(consumer(c) for c in last.tool_calls)
            ):
                return TriggerDecision(triggered=False)
        else:
            return TriggerDecision(triggered=False)

        await core.ensure_ir_ready(host, context.state)
        cache = ir_state(host)
        session = cache.get("session") or {}
        snapshot = session.get("snapshot") or {}
        if (
            cache.get("ir_turn_suppressed")
            or cache.get("ir_fallback")
            or not snapshot.get("task_id")
        ):
            return TriggerDecision(triggered=False)
        queue = pending_questions(
            cache.get("ir_field_values", []),
            params.get(
                "hitl_blocking_fields",
                ["identity", "sources", "time_context", "output_features"],
            ),
        )
        counters = asking_state(context.state, snapshot["task_id"])
        cap = limits(params)
        # A refreshed field still unresolved after an explicit reply is not asked
        # again. Preserve the legacy confirmed-only escape hatch.
        original = queue
        queue = [q for q in queue if not counters["fields"].get(q["field_id"])]
        fingerprint = hashlib.sha256(
            json.dumps(queue, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        exhausted = (
            counters["turns"] >= min(cap["budget"], cap["fires"])
            or counters["fingerprints"].get(fingerprint, 0) >= cap["fingerprint"]
            or any(counters["fields"].get(q["field_id"], 0) >= cap["field"] for q in queue)
        )
        cache["ir_hitl_best_effort"] = bool(original and (exhausted or len(queue) < len(original)))
        if not queue or exhausted:
            return TriggerDecision(triggered=False)
        return TriggerDecision(
            reason="DataTaskIR 关键口径尚未确认",
            payload={
                "queue": queue,
                "fingerprint": fingerprint,
                "task_id": snapshot["task_id"],
                "query": session["query"],
                "query_index": len(snapshot.get("queries", [])),
            },
        )

    def interaction_builder(self, context, *, params):
        lines = ["【DataTaskIR 待澄清口径】请确认具体方案，不要仅回答‘自主判断’："]
        for item in context.decision.payload["queue"]:
            lines.append(f"【{item['title']}】{item['question']}")
            lines.append("未决点：" + "；".join(map(str, item["unresolved"][:3])))
        return InteractionRequest(question="\n".join(lines)[:2000])

    def resume_handler(self, context, *, params):
        payload = context.decision.payload
        current = asking_state(context.state, payload["task_id"])
        current["query"] = payload["query"]
        current["turns"] += 1
        fingerprint = payload["fingerprint"]
        current["fingerprints"][fingerprint] = current["fingerprints"].get(fingerprint, 0) + 1
        for item in payload["queue"]:
            name = item["field_id"]
            current["fields"][name] = current["fields"].get(name, 0) + 1
        current["answers"].append(
            {
                "id": f"{payload['task_id']}:{fingerprint}:{current['turns']}",
                "content": context.answer,
                "query_index": payload["query_index"],
            }
        )
        return ResumeDecision(
            action="replan",
            state_update={"ir_clarification": current},
            message=f"用户澄清：{context.answer}\n请重新提取并检查 IR，再决定后续操作。"
            "若仍有未决项，基于已确认口径生成 SQL，并在交付说明中列出缺口。",
        )
