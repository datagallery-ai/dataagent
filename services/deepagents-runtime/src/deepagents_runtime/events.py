from __future__ import annotations

import json
from typing import Any

PROVIDER = "deepagents"
CONTRACT_VERSION = "v1"
RUNTIME_BOUND_EVENT = "runtime.bound"
INTERRUPT_EVENT_NAME = "on_interrupt"
AGENT_INTERRUPT_TYPE = "agent_interrupt"
ASK_USER = "ask_user"
SUBMIT_PLAN = "submit_plan"
PLAN_TOOL_NAMES = frozenset({"write_todos", "submit_plan"})


def encode_sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def run_started(thread_id: str, run_id: str, *, timestamp: int) -> dict[str, Any]:
    return {
        "type": "RUN_STARTED",
        "threadId": thread_id,
        "runId": run_id,
        "timestamp": timestamp,
    }


def run_finished(
    thread_id: str,
    run_id: str,
    *,
    timestamp: int,
    status: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "RUN_FINISHED",
        "threadId": thread_id,
        "runId": run_id,
        "timestamp": timestamp,
    }
    if status is not None:
        event["status"] = status
    return event


def run_error(message: str, *, timestamp: int) -> dict[str, Any]:
    return {"type": "RUN_ERROR", "message": message, "timestamp": timestamp}


def runtime_bound(run_id: str, checkpoint_ref: str, *, timestamp: int) -> dict[str, Any]:
    return {
        "type": "CUSTOM",
        "name": RUNTIME_BOUND_EVENT,
        "value": {
            "provider": PROVIDER,
            "version": CONTRACT_VERSION,
            "checkpointRef": checkpoint_ref,
        },
        "timestamp": timestamp,
    }


def text_message_start(message_id: str, *, timestamp: int) -> dict[str, Any]:
    return {
        "type": "TEXT_MESSAGE_START",
        "messageId": message_id,
        "role": "assistant",
        "timestamp": timestamp,
    }


def text_message_content(message_id: str, delta: str, *, timestamp: int) -> dict[str, Any]:
    return {
        "type": "TEXT_MESSAGE_CONTENT",
        "messageId": message_id,
        "delta": delta,
        "timestamp": timestamp,
    }


def text_message_end(message_id: str, *, timestamp: int) -> dict[str, Any]:
    return {
        "type": "TEXT_MESSAGE_END",
        "messageId": message_id,
        "timestamp": timestamp,
    }


def text_reply_events(message_id: str, text: str, *, timestamp: int) -> list[dict[str, Any]]:
    return [
        text_message_start(message_id, timestamp=timestamp),
        text_message_content(message_id, text, timestamp=timestamp),
        text_message_end(message_id, timestamp=timestamp),
    ]


def tool_call_start(tool_call_id: str, tool_name: str, *, timestamp: int) -> dict[str, Any]:
    return {
        "type": "TOOL_CALL_START",
        "toolCallId": tool_call_id,
        "toolCallName": tool_name,
        "timestamp": timestamp,
    }


def tool_call_args(tool_call_id: str, delta: str, *, timestamp: int) -> dict[str, Any]:
    return {
        "type": "TOOL_CALL_ARGS",
        "toolCallId": tool_call_id,
        "delta": delta,
        "timestamp": timestamp,
    }


def tool_call_end(tool_call_id: str, tool_name: str, *, timestamp: int) -> dict[str, Any]:
    return {
        "type": "TOOL_CALL_END",
        "toolCallId": tool_call_id,
        "toolCallName": tool_name,
        "timestamp": timestamp,
    }


def tool_call_result(tool_call_id: str, tool_name: str, content: str, *, timestamp: int) -> dict[str, Any]:
    return {
        "type": "TOOL_CALL_RESULT",
        "toolCallId": tool_call_id,
        "toolCallName": tool_name,
        "content": content,
        "messageId": f"msg_tool_{tool_call_id}",
        "role": "tool",
        "timestamp": timestamp,
    }


def map_interrupt_tool_name(name: str) -> str:
    if name == ASK_USER:
        return ASK_USER
    if name in PLAN_TOOL_NAMES:
        return SUBMIT_PLAN
    return ASK_USER


def first_action_request(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        requests = value.get("action_requests")
        if isinstance(requests, list) and requests:
            first = requests[0]
            if isinstance(first, dict):
                return first
        action = value.get("action")
        if isinstance(action, dict):
            return action
        if value.get("name") or value.get("toolName"):
            return value
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            inner = first_action_request(first.get("value", first))
            return inner or (first if first.get("name") else None)
        return first_action_request(getattr(first, "value", None))
    raw = getattr(value, "value", None)
    if raw is not None and raw is not value:
        return first_action_request(raw)
    return None


def build_interrupt_event(
    run_id: str,
    value: Any,
    *,
    timestamp: int,
    tool_call_id: str | None = None,
) -> dict[str, Any]:
    action = first_action_request(value) or {}
    name = str(action.get("name") or action.get("toolName") or ASK_USER)
    args = action.get("args")
    if args is None:
        args = action.get("suspendPayload") or {}
    tool_name = map_interrupt_tool_name(name)
    tool_call_id = str(
        tool_call_id
        or action.get("id")
        or action.get("toolCallId")
        or f"call_{name}_{run_id}"
    )
    payload = args if isinstance(args, dict) else {"value": args}
    return {
        "type": "CUSTOM",
        "name": INTERRUPT_EVENT_NAME,
        "value": {
            "type": AGENT_INTERRUPT_TYPE,
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "runId": run_id,
            "args": payload,
            "suspendPayload": payload,
            "resumeSchema": {"type": "object"},
        },
        "timestamp": timestamp,
    }
