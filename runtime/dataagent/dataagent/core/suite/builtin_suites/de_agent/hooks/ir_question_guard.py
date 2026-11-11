# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Planner post-hook: force HITL when DataTaskIR has blocking unresolved questions.

When ``update_ir`` leaves a non-empty ``ir_hitl_queue`` and human feedback is
enabled with remaining budget, rewrite the planner turn into
``request_human_feedback`` so the multi-turn HITL loop clarifies IR before SQL.

Triggers when the planner is about to finish (``complete=True``) or is calling
an IR consumer tool (nl2sql / validate). Interactive and unattended HITL both
apply (unlike ``text_question_guard``, which is unattended-only).

注意：本文件具双重模块身份——运行期经 suite-root 单文件直载（hooks.yaml 条目
``hooks.ir_question_guard.ir_question_guard``），测试/工具经包路径 import。
禁止模块级 mutable 状态，不支持包内相对导入。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage
from loguru import logger

from dataagent.actions.tools.hooks.examples.data_task_ir_spike.render import (
    format_ir_hitl_pending_action,
)
from dataagent.actions.tools.hooks.examples.data_task_ir_spike import session as ir_session
from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.hooks.hitl_common import (
    HUMAN_FEEDBACK_TOOL_NAME,
    get_hitl_budget,
    get_hitl_turn_count,
)
from dataagent.core.flex.workflow.state import FlexState

# Keep in sync with ir_hooks IR_HITL_* / IR_QUESTION_GUARD_* (avoid importing ir_hooks).
_IR_HITL_QUEUE_KEY = "ir_hitl_queue"
_IR_HITL_FEEDBACK_DIGEST_KEY = "ir_hitl_feedback_digest"
_IR_HITL_FINGERPRINT_KEY = "ir_hitl_fingerprint"
_IR_HITL_FINGERPRINT_ASK_COUNTS_KEY = "ir_hitl_fingerprint_ask_counts"
_IR_HITL_FIELD_ASK_COUNTS_KEY = "ir_hitl_field_ask_counts"
_IR_HITL_CLEARED_FIELDS_KEY = "ir_hitl_cleared_fields"
_IR_HITL_BEST_EFFORT_KEY = "ir_hitl_best_effort"
# Defaults; overridden by DATA_TASK_IR.hitl_max_same_fingerprint_asks /
# DATA_TASK_IR.hitl_max_same_field_asks / DATA_TASK_IR.hitl_question_guard_max_fires
# when present (keep in sync with ir_hooks).
_IR_HITL_MAX_SAME_FINGERPRINT_ASKS = 2
_IR_HITL_MAX_SAME_FIELD_ASKS = 2
_GUARD_FIRE_COUNT_KEY = "ir_question_guard.fire_count"
_MAX_GUARD_FIRES = 2
_MAX_PENDING_ACTION_CHARS = 2000
_IR_CONSUMER_TOOLS = frozenset({
    "wrapped_nl2sql_sub_agent_tool",
    "nl2sql_sub_agent_tool",
    "validate_deliverables",
})


def ir_question_guard(
    state: FlexState,
    runtime: Runtime | None,
    *,
    original_state: FlexState | None = None,
) -> FlexState:
    """Inject request_human_feedback when IR pending clarifications block progress."""
    result = state
    if not _ir_hitl_enabled(runtime):
        return result
    if not _is_human_feedback_enabled(result, original_state, runtime):
        return result
    if not _budget_remaining(result, original_state):
        return result
    max_fires = _question_guard_max_fires(runtime)
    if _guard_fires(runtime) >= max_fires:
        return result

    queue = _pending_queue(runtime)
    if not queue:
        return result
    # Queue may still list pre-answer blockers after the user already replied;
    # let the consumer refresh IR instead of re-asking the same questions.
    if _queue_stale_after_feedback(runtime, result):
        logger.info(
            "[ir_question_guard] skip rewrite: ir_hitl_queue predates latest HITL feedback"
        )
        return result
    max_fingerprint_asks = _max_same_fingerprint_asks(runtime)
    if _fingerprint_asks_exhausted(runtime, max_asks=max_fingerprint_asks):
        _set_best_effort(runtime)
        logger.info(
            "[ir_question_guard] skip rewrite: same hitl fingerprint already asked "
            f">={max_fingerprint_asks} times (confirmed-only best-effort)"
        )
        return result
    max_field_asks = _max_same_field_asks(runtime)
    if _field_asks_exhausted(runtime, queue, max_asks=max_field_asks):
        _set_best_effort(runtime)
        _clear_queue_fields(runtime, queue)
        logger.info(
            "[ir_question_guard] skip rewrite: same hitl field already asked "
            f">={max_field_asks} times (confirmed-only best-effort)"
        )
        return result

    messages = result.get("messages")
    last_ai = _last_ai_message(messages)
    if last_ai is None:
        return result
    if _has_human_feedback_call(last_ai):
        return result
    if not _should_trigger(result, last_ai):
        return result

    pending = format_ir_hitl_pending_action(queue, max_chars=_MAX_PENDING_ACTION_CHARS)
    updated_last = _with_human_feedback_tool_call(last_ai, pending)
    updated_messages = _replace_last_ai(messages, updated_last)
    if updated_messages is None:
        return result

    fires = _guard_fires(runtime) + 1
    _bump_fingerprint_ask(runtime)
    _bump_field_asks(runtime, queue)
    logger.warning(
        f"[ir_question_guard] DataTaskIR 待澄清 {len(queue)} 项"
        f"（第 {fires}/{max_fires} 次），转换为 request_human_feedback"
    )
    _set_guard_fires(runtime, fires)
    return {
        **result,
        "messages": updated_messages,
        "complete": False,
        "need_human_feedback": True,
        "__hitl_in_current_turn__": False,
    }


def _should_trigger(result: Mapping[str, Any], last_ai: AIMessage) -> bool:
    if bool(result.get("complete")):
        return True
    names = {
        call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
        for call in list(last_ai.tool_calls or [])
    }
    return bool(names & _IR_CONSUMER_TOOLS)


def _pending_queue(runtime: Runtime | None) -> list[dict[str, Any]]:
    get_cache = getattr(runtime, "get_cache", None)
    if not callable(get_cache):
        return []
    try:
        queue = get_cache(_IR_HITL_QUEUE_KEY, []) or []
    except Exception:
        return []
    return queue if isinstance(queue, list) else []


def _cache_get(runtime: Runtime | None, key: str, default: Any = None) -> Any:
    get_cache = getattr(runtime, "get_cache", None)
    if not callable(get_cache):
        return default
    try:
        return get_cache(key, default)
    except Exception:
        return default


def _cache_set(runtime: Runtime | None, key: str, value: Any) -> None:
    set_cache = getattr(runtime, "set_cache", None)
    if callable(set_cache):
        set_cache(key, value)


def _data_task_ir_option(runtime: Runtime | None, name: str, default: Any) -> Any:
    getter = getattr(runtime, "get_config", None) if runtime is not None else None
    if callable(getter):
        try:
            return getter(f"DATA_TASK_IR.{name}", default)
        except Exception:
            return default
    get_all = getattr(runtime, "get_all_config", None) if runtime is not None else None
    if not callable(get_all):
        return default
    try:
        config = get_all() or {}
        section = config.get("DATA_TASK_IR", {}) if isinstance(config, Mapping) else {}
        if isinstance(section, Mapping) and name in section:
            return section.get(name)
    except Exception:
        return default
    return default


def _ir_hitl_enabled(runtime: Runtime | None) -> bool:
    """DATA_TASK_IR.enable_ir_hitl; default true. False restores pre-IR→HITL behavior."""
    raw = _data_task_ir_option(runtime, "enable_ir_hitl", True)
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    return bool(raw)


def _positive_int_option(runtime: Runtime | None, name: str, default: int) -> int:
    raw = _data_task_ir_option(runtime, name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, value)


def _max_same_fingerprint_asks(runtime: Runtime | None) -> int:
    return _positive_int_option(
        runtime, "hitl_max_same_fingerprint_asks", _IR_HITL_MAX_SAME_FINGERPRINT_ASKS
    )


def _max_same_field_asks(runtime: Runtime | None) -> int:
    return _positive_int_option(
        runtime, "hitl_max_same_field_asks", _IR_HITL_MAX_SAME_FIELD_ASKS
    )


def _question_guard_max_fires(runtime: Runtime | None) -> int:
    return _positive_int_option(
        runtime, "hitl_question_guard_max_fires", _MAX_GUARD_FIRES
    )


def _fingerprint_asks_exhausted(
    runtime: Runtime | None,
    *,
    max_asks: int | None = None,
) -> bool:
    fingerprint = str(_cache_get(runtime, _IR_HITL_FINGERPRINT_KEY, "") or "")
    if not fingerprint:
        return False
    raw = _cache_get(runtime, _IR_HITL_FINGERPRINT_ASK_COUNTS_KEY, {}) or {}
    if not isinstance(raw, dict):
        return False
    try:
        asks = int(raw.get(fingerprint, 0) or 0)
    except (TypeError, ValueError):
        asks = 0
    limit = max_asks if max_asks is not None else _max_same_fingerprint_asks(runtime)
    return asks >= limit


def _queue_field_ids(queue: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for item in queue:
        if not isinstance(item, dict):
            continue
        field_id = str(item.get("field_id") or "").strip()
        if field_id:
            ids.append(field_id)
    return ids


def _field_ask_counts(runtime: Runtime | None) -> dict[str, int]:
    raw = _cache_get(runtime, _IR_HITL_FIELD_ASK_COUNTS_KEY, {}) or {}
    if not isinstance(raw, dict):
        return {}
    counts: dict[str, int] = {}
    for key, value in raw.items():
        try:
            counts[str(key)] = int(value or 0)
        except (TypeError, ValueError):
            counts[str(key)] = 0
    return counts


def _cleared_fields(runtime: Runtime | None) -> set[str]:
    raw = _cache_get(runtime, _IR_HITL_CLEARED_FIELDS_KEY, []) or []
    if isinstance(raw, (set, frozenset)):
        return {str(item) for item in raw if str(item)}
    if isinstance(raw, dict):
        return {str(key) for key in raw if str(key)}
    if isinstance(raw, (list, tuple)):
        return {str(item) for item in raw if str(item)}
    return set()


def _field_asks_exhausted(
    runtime: Runtime | None,
    queue: list[dict[str, Any]],
    *,
    max_asks: int | None = None,
) -> bool:
    field_ids = _queue_field_ids(queue)
    if not field_ids:
        return False
    cleared = _cleared_fields(runtime)
    if set(field_ids) <= cleared:
        return True
    counts = _field_ask_counts(runtime)
    limit = max_asks if max_asks is not None else _max_same_field_asks(runtime)
    return all(counts.get(field_id, 0) >= limit for field_id in field_ids)


def _bump_fingerprint_ask(runtime: Runtime | None) -> None:
    fingerprint = str(_cache_get(runtime, _IR_HITL_FINGERPRINT_KEY, "") or "")
    if not fingerprint:
        return
    raw = _cache_get(runtime, _IR_HITL_FINGERPRINT_ASK_COUNTS_KEY, {}) or {}
    counts = dict(raw) if isinstance(raw, dict) else {}
    try:
        current = int(counts.get(fingerprint, 0) or 0)
    except (TypeError, ValueError):
        current = 0
    counts[fingerprint] = current + 1
    _cache_set(runtime, _IR_HITL_FINGERPRINT_ASK_COUNTS_KEY, counts)


def _bump_field_asks(runtime: Runtime | None, queue: list[dict[str, Any]]) -> None:
    counts = _field_ask_counts(runtime)
    changed = False
    for field_id in _queue_field_ids(queue):
        counts[field_id] = int(counts.get(field_id, 0) or 0) + 1
        changed = True
    if changed:
        _cache_set(runtime, _IR_HITL_FIELD_ASK_COUNTS_KEY, counts)


def _clear_queue_fields(runtime: Runtime | None, queue: list[dict[str, Any]]) -> None:
    cleared = _cleared_fields(runtime)
    cleared.update(_queue_field_ids(queue))
    _cache_set(runtime, _IR_HITL_CLEARED_FIELDS_KEY, sorted(cleared))
    _cache_set(runtime, _IR_HITL_QUEUE_KEY, [])


def _set_best_effort(runtime: Runtime | None) -> None:
    _cache_set(runtime, _IR_HITL_BEST_EFFORT_KEY, True)


def _feedback_digest(state: Mapping[str, Any]) -> str:
    texts: list[str] = []
    seen: set[str] = set()
    for message in state.get("messages") or []:
        text = ir_session.feedback_content(message)
        if not text:
            continue
        key = str(getattr(message, "tool_call_id", None) or text)
        if key in seen:
            continue
        seen.add(key)
        texts.append(text)
    serialized = json.dumps(texts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _queue_stale_after_feedback(runtime: Runtime | None, state: Mapping[str, Any]) -> bool:
    """True when HITL replies arrived after the last ir_hitl_queue sync."""
    get_cache = getattr(runtime, "get_cache", None)
    if not callable(get_cache):
        return False
    current = _feedback_digest(state)
    empty = hashlib.sha256(
        json.dumps([], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    try:
        stored = get_cache(_IR_HITL_FEEDBACK_DIGEST_KEY, None)
    except Exception:
        stored = None
    if stored is None:
        return current != empty
    return current != stored


def _is_human_feedback_enabled(
    result: Mapping[str, Any],
    original_state: Mapping[str, Any] | None,
    runtime: Runtime | None,
) -> bool:
    source = original_state if original_state is not None else result
    enabled = source.get("enable_human_feedback")
    if enabled is not None:
        return bool(enabled)
    get_all_config = getattr(runtime, "get_all_config", None) if runtime is not None else None
    if not callable(get_all_config):
        return False
    try:
        config = get_all_config()
        agent_config = config.get("AGENT_CONFIG", {}) if isinstance(config, Mapping) else {}
        return bool(agent_config.get("enable_human_feedback", False))
    except Exception:
        return False


def _budget_remaining(
    result: Mapping[str, Any],
    original_state: Mapping[str, Any] | None,
) -> bool:
    source = original_state if original_state is not None else result
    return get_hitl_turn_count(source) < get_hitl_budget(source)


def _guard_fires(runtime: Runtime | None) -> int:
    get_cache = getattr(runtime, "get_cache", None)
    if not callable(get_cache):
        return 0
    try:
        return int(get_cache(_GUARD_FIRE_COUNT_KEY, 0))
    except Exception:
        return 0


def _set_guard_fires(runtime: Runtime | None, count: int) -> None:
    set_cache = getattr(runtime, "set_cache", None)
    if callable(set_cache):
        set_cache(_GUARD_FIRE_COUNT_KEY, count)


def _last_ai_message(messages: Any) -> AIMessage | None:
    if isinstance(messages, AIMessage):
        return messages
    if isinstance(messages, list):
        for msg in reversed(messages):
            if isinstance(msg, AIMessage):
                return msg
    return None


def _has_human_feedback_call(message: AIMessage) -> bool:
    for call in list(message.tool_calls or []) + list(message.invalid_tool_calls or []):
        name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
        if name == HUMAN_FEEDBACK_TOOL_NAME:
            return True
    return False


def _replace_last_ai(messages: Any, updated: AIMessage) -> list[Any] | None:
    if isinstance(messages, AIMessage):
        return [updated]
    if not isinstance(messages, list):
        return None
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], AIMessage):
            new_messages = list(messages)
            new_messages[index] = updated
            return new_messages
    return None


def _with_human_feedback_tool_call(message: AIMessage, pending_action: str) -> AIMessage:
    """Replace the planner turn with a single HITL ask.

    Drop all original tool_calls (and invalid_tool_calls). Router skips Executor when
    need_human_feedback is set, so any retained call would leave a dangling
    tool_call_id without a ToolMessage and break the next OpenAI-compatible turn.
    """
    tool_call = {
        "id": f"call_ir_guard_{uuid.uuid4().hex[:12]}",
        "name": HUMAN_FEEDBACK_TOOL_NAME,
        "args": {
            "reason": (
                "DataTaskIR 未确认口径，需用户澄清后再生成/校验 SQL；"
                "请给出倾向方案与 1-2 个具体候选。"
            ),
            "pending_action": pending_action,
        },
    }
    return AIMessage(
        content=message.content,
        additional_kwargs=dict(message.additional_kwargs or {}),
        response_metadata=dict(message.response_metadata or {}),
        id=message.id,
        name=message.name,
        tool_calls=[tool_call],
        invalid_tool_calls=[],
        usage_metadata=message.usage_metadata,
    )
