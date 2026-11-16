"""Checkpointed business clarification, with separate human and evaluation paths."""

import asyncio
import hashlib
import json
from pathlib import Path
from threading import Lock
from typing import Annotated, NotRequired

from langchain.agents import AgentState
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from dataagent.model_stream import collect_model_stream
from utils.data_task_ir.render import collect_ir_hitl_questions
from utils.data_task_ir.runtime import ir_enabled, ir_options, ir_state
from utils.hitl_auto_resolver import hitl_auto_resolver
from utils.hitl_common import (
    DEFAULT_HITL_BUDGET,
    HUMAN_FEEDBACK_TOOL_NAME,
    get_hitl_budget,
    normalize_question,
)


class HITLState(AgentState):
    hitl: NotRequired[Annotated[dict, PrivateStateAttr]]


def options(context):
    return context.get_plugin_config("de-agent").get("hitl", {})


def enabled(context):
    return options(context).get("enabled", False) is True


def initial(context, state):
    config = options(context)
    budget = get_hitl_budget({"hitl_budget": config.get("budget", DEFAULT_HITL_BUDGET)})
    previous = state.get("hitl", {})
    query = next(
        (
            str(m.content)
            for m in reversed(state.get("messages", []))
            if isinstance(m, HumanMessage)
        ),
        "",
    )
    return {
        "count": 0,
        "query": query,
        "history": [],
        "field_asks": {},
        "fingerprint_asks": {},
        "guard_fires": 0,
        "ir_guard_fires": 0,
        "cleared_fields": [],
        **previous,
        "budget": budget,
    }


def history_reply(data, question):
    norm = normalize_question(question)
    return next((item["answer"] for item in data["history"] if item["question_norm"] == norm), None)


def answer_update(state, data, question, answer, tool_call_id, *, source, action="HUMAN"):
    # A resumed/replayed tool cannot count a completed answer twice.
    if any(item["tool_call_id"] == tool_call_id for item in data["history"]):
        return None
    history = [
        *data["history"],
        {
            "tool_call_id": tool_call_id,
            "question": question,
            "question_norm": normalize_question(question),
            "answer": answer,
            "source": source,
            "action": action,
        },
    ]
    return {
        "hitl": {**data, "count": data["count"] + 1, "history": history},
        "messages": [
            ToolMessage(
                content=answer,
                tool_call_id=tool_call_id,
                name=HUMAN_FEEDBACK_TOOL_NAME,
                response_metadata={"hitl": {"source": source, "action": action}},
            )
        ],
    }


def budget_message(data, context=None):
    remaining = max(0, data["budget"] - data["count"])
    instructions = (Path(__file__).resolve().parents[1] / "prompts/hitl.md").read_text(
        encoding="utf-8"
    )
    conditions = options(context).get("human_feedback_conditions", []) if context else []
    if isinstance(conditions, str):
        conditions = [conditions]
    if isinstance(conditions, list):
        instructions += "\n" + "\n".join(
            item.strip() + "；请调用 request_human_feedback，给出倾向方案及具体候选。"
            for item in conditions
            if isinstance(item, str) and item.strip()
        )
    return SystemMessage(
        id="hitl-budget",
        content=(
            f"HITL 交互预算：已用 {data['count']}/{data['budget']}，剩余 {remaining} 次。"
            "只询问最阻塞的未决业务口径；提出倾向方案及 1–2 个具体候选。"
            "已确认的问题不得重复询问。预算耗尽后仅基于已确认信息继续，说明未决项，"
            "不能把自主判断或系统兜底说成用户确认。\n\n" + instructions
        ),
    )


def task_workspace(context, data):
    key = str(data.get("task_key") or options(context).get("task_id") or context.session.root.name)
    return context.session.outputs / ".hitl" / hashlib.sha256(key.encode()).hexdigest()[:24]


class EvaluationRuntime:
    """Translate only resolver dependencies; golden stays in trusted configuration."""

    def __init__(self, context, config, data):
        self.context = context
        self.config = config
        self.workspace_dir = str(task_workspace(context, data))
        self.loop = asyncio.get_running_loop()
        self.pending = set()
        self.lock = Lock()
        self.cancelled = False

    def get_all_config(self):
        config = self.config
        return {
            "AGENT_CONFIG": {
                "hitl_auto_resolve": True,
                "hitl_task_index_path": config.get("task_index_path"),
                "hitl_golden_sql_path": config.get("golden_sql_path"),
                "hitl_golden_schema_path": config.get("golden_schema_path"),
                "hitl_eval_task_name": config.get("eval_task_name"),
                "hitl_golden_sql_dir": config.get("golden_sql_dir"),
            }
        }

    def llm(self, _):
        model = self.context.get_model(self.config.get("model", "primary"))
        owner = self

        class ModelBridge:
            def invoke(self, messages):
                with owner.lock:
                    if owner.cancelled:
                        raise RuntimeError("HITL model call was cancelled")
                    future = asyncio.run_coroutine_threadsafe(
                        collect_model_stream(model, messages), owner.loop
                    )
                    owner.pending.add(future)
                try:
                    return future.result()
                finally:
                    with owner.lock:
                        owner.pending.discard(future)

        return ModelBridge()

    def cancel(self):
        with self.lock:
            self.cancelled = True
            for future in self.pending:
                future.cancel()


async def auto_answer(context, state, data, question, call):

    config = options(context)
    ids = {item["tool_call_id"] for item in data["history"]} | {call}
    selected = [
        m
        for m in state.get("messages", [])
        if (
            getattr(m, "tool_call_id", None) in ids
            or any(c.get("id") in ids for c in getattr(m, "tool_calls", []))
        )
    ]
    resolver_state = {
        **state,
        "hitl_auto_resolve": True,
        "hitl_count": data["count"],
        "hitl_budget": data["budget"],
        "messages": [HumanMessage(content=data["query"]), *selected],
    }
    adapter = EvaluationRuntime(context, config, data)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(hitl_auto_resolver, resolver_state, adapter),
            timeout=float(config.get("timeout_seconds", 60)),
        )
    finally:
        adapter.cancel()
    info = result.get("hitl_resolved_info", {})
    reply = next(
        (
            m
            for m in reversed(result.get("messages", []))
            if isinstance(m, ToolMessage) and m.tool_call_id == call
        ),
        None,
    )
    return (
        (str(reply.content), info.get("action", "FALLBACK"))
        if reply
        else ("请基于已有元数据和题意自主判断并继续，无需再就此事询问。", "FALLBACK")
    )


def ir_questions(context, data):

    if not ir_enabled(context) or ir_options(context).get("enable_ir_hitl", True) is False:
        return []
    cache = ir_state(context)
    if cache.get("ir_turn_suppressed") or cache.get("ir_fallback"):
        return []
    config = ir_options(context)
    fields = config.get("hitl_blocking_fields")
    queue = collect_ir_hitl_questions(cache.get("ir_field_values", []), blocking_fields=fields)
    has_unresolved = bool(queue)
    history_digest = hashlib.sha256(
        json.dumps(data["history"], sort_keys=True).encode()
    ).hexdigest()
    old_digest = data.get("ir_feedback_digest")
    # An answered field still unresolved after refresh is released, not re-asked.
    cleared = set(data.get("cleared_fields", []))
    if old_digest and old_digest != history_digest:
        cleared.update(data.get("last_ir_fields", []))
    data["ir_feedback_digest"] = history_digest
    data["cleared_fields"] = sorted(cleared)
    queue = [
        q
        for q in queue
        if q["field_id"] not in cleared
        and data["field_asks"].get(q["field_id"], 0) < config.get("hitl_max_same_field_asks", 2)
    ]
    fingerprint = hashlib.sha256(json.dumps(queue, sort_keys=True).encode()).hexdigest()
    if (
        data["count"] >= data["budget"]
        or data.get("ir_guard_fires", 0) >= config.get("hitl_question_guard_max_fires", 2)
        or data["fingerprint_asks"].get(fingerprint, 0)
        >= config.get("hitl_max_same_fingerprint_asks", 2)
    ):
        queue = []
    data["ir_queue"] = queue
    data["ir_fingerprint"] = fingerprint
    data["ir_best_effort"] = bool(has_unresolved and not queue)
    cache["ir_hitl_best_effort"] = data["ir_best_effort"]
    return queue
