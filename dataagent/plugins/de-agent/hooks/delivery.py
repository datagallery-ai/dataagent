"""Native model hooks for bounded SQL delivery and paired artifacts."""

import time

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command
from utils.data_task_ir.session_store import atomic_json
from utils.evidence import output_file


def _scope(state):
    users = [
        m
        for m in state.get("messages", [])
        if isinstance(m, HumanMessage)
        and not str(m.id or "").startswith(("hitl_feedback_", "delivery_"))
    ]
    if not users:
        return ""
    import hashlib

    return hashlib.sha256(f"{len(users)}:{users[-1].id}:{users[-1].content}".encode()).hexdigest()


def _ledger(context, state):
    import json

    path = context.session.outputs / ".runtime" / "delivery-budget.json"
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        saved = {}
    scope = _scope(state)
    if saved.get("scope") != scope:
        saved = {"scope": scope, "started": time.time(), "calls": 0}
    return path, saved


def deadline(state, runtime, *, params):
    options = runtime.context.get_plugin_config("de-agent").get("delivery", {})
    budget = options.get("task_wall_budget_seconds", 0)
    limit = options.get("max_model_calls", 0)
    if not budget and not limit:
        return None
    path, saved = _ledger(runtime.context, state)
    saved["calls"] += 1
    remaining = budget - (time.time() - saved["started"]) if budget else float("inf")
    forcing = remaining <= options.get("forcing_window_seconds", 60) or (
        limit and saved["calls"] >= max(1, limit - options.get("iteration_window", 2))
    )
    if saved.get("forced") or not forcing:
        atomic_json(path, saved)
        return None
    saved["forced"] = True
    atomic_json(path, saved)
    return {
        "messages": [
            HumanMessage(
                id="delivery_deadline_" + saved["scope"],
                content=(
                    "任务接近时间或调用预算上限。停止重复检索，依据用户已确认口径尽快交付可用 SQL；"
                    "生成 create_*.sql 与 insert_*.sql，明确未解决风险，不把未知信息当作确认。"
                ),
            )
        ]
    }


def require_pair(state, runtime, *, params):
    options = runtime.context.get_plugin_config("de-agent").get("delivery", {})
    if options.get("require_sql_pair", False) is not True:
        return None
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    if not isinstance(last, AIMessage) or last.tool_calls:
        return None
    outputs = runtime.context.session.outputs
    ddl = [
        p
        for p in outputs.rglob("create_*.sql")
        if output_file(runtime.context, str(p)).is_file() and p.read_text().strip()
    ]
    dml = [
        p
        for p in outputs.rglob("insert_*.sql")
        if output_file(runtime.context, str(p)).is_file() and p.read_text().strip()
    ]
    if ddl and dml:
        return None
    path, saved = _ledger(runtime.context, state)
    if saved.get("pair_retry"):
        return None
    saved["pair_retry"] = True
    atomic_json(path, saved)
    return Command(
        goto="model",
        update={
            "messages": [
                HumanMessage(
                    id="delivery_pair_" + saved["scope"],
                    content=(
                        "交付检查：尚缺非空 create_*.sql 或 insert_*.sql。请依据已确认需求补齐成对 SQL 后再结束；"
                        "若任务仅要求命名、查重或解释，则说明不适用，不能虚构 SQL。"
                    ),
                )
            ]
        },
    )
