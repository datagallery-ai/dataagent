# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Planner post-hook that forces one final HITL confirmation before delivery."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage
from loguru import logger

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.workflow.state import FlexState

FINAL_HITL_GUARD_TRIGGERED_KEY = "__final_human_feedback_guard_triggered__"
HUMAN_FEEDBACK_TOOL_NAME = "request_human_feedback"
_RUNTIME_CACHE_KEY = "human_feedback_guard.final_hitl_triggered"


def human_feedback_guard(
    state: FlexState,
    runtime: Runtime | None,
    *,
    original_state: FlexState | None = None,
    human_feedback_guard_conditions: str | list[Any] | None = None,
) -> FlexState:
    """Inject ``request_human_feedback`` once when planner declares final completion."""
    result = state
    if not _is_human_feedback_enabled(result, original_state, runtime):
        return result
    if not result.get("complete"):
        return result
    if _final_guard_already_triggered(result, original_state, runtime):
        return result

    messages = result.get("messages")
    pending_action = _format_guard_pending_action(human_feedback_guard_conditions)
    updated_messages = _inject_human_feedback_tool_call(messages, pending_action)
    if updated_messages is None:
        return result

    logger.info("[human_feedback_guard] Planner completed; injecting final request_human_feedback")
    _mark_runtime_guard_triggered(runtime)
    return {
        **result,
        "messages": updated_messages,
        "complete": False,
        "need_human_feedback": True,
        "__hitl_in_current_turn__": False,
        FINAL_HITL_GUARD_TRIGGERED_KEY: True,
    }


def _is_human_feedback_enabled(
    result: Mapping[str, Any],
    original_state: Mapping[str, Any] | None,
    runtime: Runtime | None,
) -> bool:
    source = original_state if original_state is not None else result
    if source.get("enable_human_feedback") is not None:
        return bool(source.get("enable_human_feedback", False))
    return _runtime_human_feedback_enabled(runtime)


def _final_guard_already_triggered(
    result: Mapping[str, Any],
    original_state: Mapping[str, Any] | None,
    runtime: Runtime | None,
) -> bool:
    return bool(
        result.get(FINAL_HITL_GUARD_TRIGGERED_KEY, False)
        or (original_state or {}).get(FINAL_HITL_GUARD_TRIGGERED_KEY, False)
        or _runtime_guard_triggered(runtime)
    )


def _runtime_human_feedback_enabled(runtime: Runtime | None) -> bool:
    if runtime is None:
        return False
    get_all_config = getattr(runtime, "get_all_config", None)
    if not callable(get_all_config):
        return False
    try:
        config = get_all_config()
    except Exception:
        return False
    agent_config = config.get("AGENT_CONFIG", {}) if isinstance(config, Mapping) else {}
    return bool(agent_config.get("enable_human_feedback", False))


def _runtime_guard_triggered(runtime: Runtime | None) -> bool:
    get_cache = getattr(runtime, "get_cache", None)
    return bool(callable(get_cache) and get_cache(_RUNTIME_CACHE_KEY, False))


def _mark_runtime_guard_triggered(runtime: Runtime | None) -> None:
    set_cache = getattr(runtime, "set_cache", None)
    if callable(set_cache):
        set_cache(_RUNTIME_CACHE_KEY, True)


def _format_guard_pending_action(conditions: str | list[Any] | None) -> str:
    if conditions is None:
        return ""
    if isinstance(conditions, str):
        return conditions.strip()
    if isinstance(conditions, list):
        normalized = []
        for item in conditions:
            text = str(item).strip()
            if text:
                normalized.append(text)
        return "  \n".join(f"• {text}" for text in normalized)
    logger.warning(
        "human_feedback_guard_conditions must be a list or string; got {} — ignoring",
        type(conditions).__name__,
    )
    return ""


def _inject_human_feedback_tool_call(messages: Any, pending_action: str) -> Any | None:
    if isinstance(messages, AIMessage):
        return _with_human_feedback_tool_call(messages, pending_action)
    if isinstance(messages, list):
        return _inject_into_message_list(messages, pending_action)
    return None


def _inject_into_message_list(messages: list[Any], pending_action: str) -> list[Any] | None:
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], AIMessage):
            updated_messages = list(messages)
            updated_messages[index] = _with_human_feedback_tool_call(messages[index], pending_action)
            return updated_messages
    return None


def _with_human_feedback_tool_call(message: AIMessage, pending_action: str) -> AIMessage:
    tool_call = {
        "id": f"call_final_hitl_{uuid.uuid4().hex[:12]}",
        "name": HUMAN_FEEDBACK_TOOL_NAME,
        "args": {
            "reason": "任务已完成，请确认最终结果是否满足需求。如需调整，请说明需要修改的部分。",
            "pending_action": pending_action,
        },
    }
    return AIMessage(
        content=message.content,
        additional_kwargs=dict(message.additional_kwargs or {}),
        response_metadata=dict(message.response_metadata or {}),
        id=message.id,
        name=message.name,
        tool_calls=[*_tool_calls(message), tool_call],
        invalid_tool_calls=list(message.invalid_tool_calls or []),
        usage_metadata=message.usage_metadata,
    )


def _tool_calls(message: AIMessage) -> Sequence[Any]:
    return list(message.tool_calls or [])
