"""Clarification guards at native model boundaries, before any consumer side effect."""

from copy import deepcopy
from uuid import uuid4

from langchain_core.messages import AIMessage
from utils.data_task_ir import core
from utils.data_task_ir import session as ir_session
from utils.data_task_ir.render import format_ir_hitl_pending_action
from utils.data_task_ir.runtime import ir_enabled, ir_options, ir_state
from utils.hitl import HITLState, budget_message, enabled, initial, ir_questions, options
from utils.hitl_common import HUMAN_FEEDBACK_TOOL_NAME
from utils.hitl_text_guard import _ends_awaiting_user


async def prepare_context(state, runtime, *, params):
    if not enabled(runtime.context):
        return None
    data = initial(runtime.context, state)
    boundary = data.get("ir_turn")
    if ir_enabled(runtime.context) and boundary and not ir_state(runtime.context).get("turn"):
        # Resume does not replay before_agent. Recover the original evidence boundary,
        # so the just-received answer is not classified as historical feedback.
        ir_session.prepare_turn(runtime.context, state)
        cache = ir_state(runtime.context)
        cache["turn"].update(
            query=boundary["query"],
            started_at_ns=boundary["started_at_ns"],
            old_feedback_ids=set(boundary["old_feedback_ids"]),
        )
        restored = ir_session.current(runtime.context)
        if restored:
            if (restored.get("snapshot") or {}).get("task_id") != boundary.get("task_id"):
                raise RuntimeError("HITL checkpoint and DataTaskIR task do not match")
            restored.update(
                query=boundary["query"],
                turn_id=boundary["turn_id"],
                started_at_ns=boundary["started_at_ns"],
                old_feedback_ids=set(boundary["old_feedback_ids"]),
                decision={"relation": "continue", "reason": "恢复同一任务的人工澄清"},
            )
    session = ir_session.current(runtime.context) if ir_enabled(runtime.context) else None
    if session and session.get("decision") is None:
        await ir_session.resolve_task_context(runtime.context)
    task_key = options(runtime.context).get("task_id")
    if session and (session.get("decision") or {}).get("relation") == "new":
        task_key = session["turn_id"]
    if task_key and data.get("task_key") != task_key:
        data = initial(runtime.context, {"messages": state.get("messages", [])})
        data["task_key"] = task_key
    return {"hitl": data, "messages": [budget_message(data, runtime.context)]}


async def guard_questions(state, runtime, *, params):
    context = runtime.context
    if not enabled(context):
        return None
    last = next((m for m in reversed(state.get("messages", [])) if isinstance(m, AIMessage)), None)
    if last is None:
        return None
    calls = last.tool_calls or []
    feedback_calls = [call for call in calls if call["name"] == HUMAN_FEEDBACK_TOOL_NAME]
    if feedback_calls:
        if len(calls) > 1:
            # One blocking point per round; do not run another consumer beside a pause.
            return {"messages": [last.model_copy(update={"tool_calls": feedback_calls[:1]})]}
        return None
    data = deepcopy(initial(context, state))
    pending = ""
    consumers = [
        call
        for call in calls
        if (
            call["name"] in {"de-agent__read_ir", "de-agent__check_and_repair_sql"}
            or (call["name"] == "task" and call["args"].get("subagent_type") == "nl2sql")
        )
    ]
    if (
        (consumers or not calls)
        and ir_enabled(context)
        and ir_options(context).get("enable_ir_hitl", True) is not False
    ):
        await core.ensure_ir_ready(context, state)
        boundary = ir_state(context).get("turn")
        session = ir_session.current(context)
        if boundary:
            data["ir_turn"] = {
                "query": boundary["query"],
                "started_at_ns": boundary["started_at_ns"],
                "old_feedback_ids": list(boundary["old_feedback_ids"]),
                "turn_id": (session or {}).get("turn_id"),
                "task_id": ((session or {}).get("snapshot") or {}).get("task_id"),
            }
        queue = ir_questions(context, data)
        if queue:
            pending = format_ir_hitl_pending_action(queue)
            data["ir_guard_fires"] = data.get("ir_guard_fires", 0) + 1
            data["last_ir_fields"] = [q["field_id"] for q in queue]
            for q in queue:
                key = q["field_id"]
                data["field_asks"][key] = data["field_asks"].get(key, 0) + 1
            key = data["ir_fingerprint"]
            data["fingerprint_asks"][key] = data["fingerprint_asks"].get(key, 0) + 1
    config = options(context)
    if not pending and not calls and data["count"] < data["budget"]:
        if (
            config.get("auto_resolve", False) is True
            and data.get("text_guard_fires", 0) < 2
            and _ends_awaiting_user(last.text)
        ):
            pending = last.text[:2000]
            data["text_guard_fires"] = data.get("text_guard_fires", 0) + 1
        elif config.get("final_confirmation", False) is True and not data.get("final_asked"):
            pending = "本次交付方案：\n" + last.text[:1800] + "\n请确认以上具体方案及交付文件，或指出需要调整的字段、口径或文件。"
            data["final_asked"] = True
    if not pending:
        return {"hitl": data}
    data["guard_fires"] += 1
    rewritten = last.model_copy(
        update={
            "content": "",
            "tool_calls": [
                {
                    "name": HUMAN_FEEDBACK_TOOL_NAME,
                    "id": "hitl_" + uuid4().hex,
                    "type": "tool_call",
                    "args": {"reason": "继续执行前需要澄清业务口径", "pending_action": pending},
                }
            ],
        }
    )
    return {"hitl": data, "messages": [rewritten]}


prepare_context.state_schema = HITLState
guard_questions.state_schema = HITLState
