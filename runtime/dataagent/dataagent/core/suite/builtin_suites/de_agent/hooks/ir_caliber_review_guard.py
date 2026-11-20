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
"""Planner post-hook: one-shot pre-SQL DataTaskIR caliber review.

After blocking IR→HITL clarifications are cleared (or fused), **or** after
``ir_question_guard`` has exhausted its fire cap (pending queue may remain while
best-effort SQL would otherwise proceed), and before the first nl2sql consume,
optionally rewrite the planner turn into ``request_human_feedback`` so the user
can confirm or supplement confirmed口径 (filters the fill path would not
proactively ask for).

If mid-flow HITL (e.g. table confirm) has already exhausted ``hitl_budget``, this
guard still reserves **one** caliber-review ask when none has been asked yet;
``hitl_auto_resolver`` likewise exempts that one-shot from ``BUDGET_EXHAUSTED``
force-converge so the reserved ask is actually answered.

Default off (``DATA_TASK_IR.enable_ir_caliber_review``). Runs at most once per task
after any reply (confirm or supplement); no second review round.

注意：本文件具双重模块身份——运行期经 suite-root 单文件直载（hooks.yaml 条目
``hooks.ir_caliber_review_guard.ir_caliber_review_guard``），测试/工具经包路径 import。
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
    format_ir_caliber_review_pending,
)
from dataagent.actions.tools.hooks.examples.data_task_ir_spike import session as ir_session
from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.hooks.hitl_common import (
    HUMAN_FEEDBACK_TOOL_NAME,
    get_hitl_budget,
    get_hitl_turn_count,
)
from dataagent.core.flex.workflow.state import FlexState

# Keep in sync with ir_hooks IR_CALIBER_REVIEW_* / IR_HITL_* / IR_QUESTION_GUARD_*
# (avoid importing ir_hooks).
_IR_HITL_QUEUE_KEY = "ir_hitl_queue"
_IR_HITL_BEST_EFFORT_KEY = "ir_hitl_best_effort"
_IR_FIELD_VALUES_KEY = "ir_field_values"
_QUESTION_GUARD_FIRE_COUNT_KEY = "ir_question_guard.fire_count"
_DEFAULT_QUESTION_GUARD_MAX_FIRES = 2
_REVIEW_DONE_KEY = "ir_caliber_review.done"
_REVIEW_ASKED_KEY = "ir_caliber_review.asked"
_REVIEW_FIRE_COUNT_KEY = "ir_caliber_review.fire_count"
_REVIEW_ASK_DIGEST_KEY = "ir_caliber_review.ask_feedback_digest"
_DEFAULT_MAX_FIRES = 1
_MAX_PENDING_ACTION_CHARS = 3200
_NL2SQL_TOOLS = frozenset({
    "wrapped_nl2sql_sub_agent_tool",
    "nl2sql_sub_agent_tool",
})


def ir_caliber_review_guard(
    state: FlexState,
    runtime: Runtime | None,
    *,
    original_state: FlexState | None = None,
) -> FlexState:
    """Inject one-shot caliber-review HITL before first SQL generation."""
    result = state
    if not _caliber_review_enabled(runtime):
        return result
    if not _is_human_feedback_enabled(result, original_state, runtime):
        return result
    if _is_done(runtime):
        return result

    _maybe_complete_after_reply(runtime, result)

    if _is_done(runtime):
        return result

    budget_ok = _budget_remaining(result, original_state)
    reserved_after_budget = False
    if not budget_ok:
        # Mid-flow asks (table confirm, etc.) may burn hitl_budget before the
        # first nl2sql. Still allow one reserved caliber ask if never asked.
        if _cache_get(runtime, _REVIEW_ASKED_KEY) or _fire_count(runtime) >= _max_fires(runtime):
            _mark_done(runtime, "hitl budget exhausted")
            return result
        reserved_after_budget = True

    queue_pending = _pending_blocking_queue(runtime)
    allow_despite_queue = _allow_review_despite_pending_queue(runtime)
    if queue_pending and not allow_despite_queue:
        return result
    max_fires = _max_fires(runtime)
    if _fire_count(runtime) >= max_fires:
        _mark_done(runtime, "caliber review fire cap reached")
        return result

    fields = _field_values(runtime)
    if not fields:
        return result
    # Queue may still list unresolved items after question-guard fire cap; treat
    # as best-effort confirmed-only summary (same as ensure_ir_ready path).
    best_effort = bool(_cache_get(runtime, _IR_HITL_BEST_EFFORT_KEY)) or (
        queue_pending and allow_despite_queue
    )
    pending = format_ir_caliber_review_pending(
        fields,
        max_chars=_MAX_PENDING_ACTION_CHARS,
        best_effort=best_effort,
    )
    if not pending:
        return result

    messages = result.get("messages")
    last_ai = _last_ai_message(messages)
    if last_ai is None:
        return result
    if _has_human_feedback_call(last_ai):
        return result
    if not _should_trigger(result, last_ai):
        return result

    updated_last = _with_human_feedback_tool_call(last_ai, pending)
    updated_messages = _replace_last_ai(messages, updated_last)
    if updated_messages is None:
        return result

    fires = _fire_count(runtime) + 1
    _cache_set(runtime, _REVIEW_FIRE_COUNT_KEY, fires)
    _cache_set(runtime, _REVIEW_ASKED_KEY, True)
    _cache_set(runtime, _REVIEW_ASK_DIGEST_KEY, _feedback_digest(result))
    if reserved_after_budget:
        logger.info(
            "[ir_caliber_review_guard] HITL budget exhausted; "
            "proceeding with reserved one-shot caliber review"
        )
    if queue_pending and allow_despite_queue:
        logger.info(
            "[ir_caliber_review_guard] proceeding despite pending ir_hitl_queue "
            "(question-guard exhausted or best-effort)"
        )
    logger.warning(
        f"[ir_caliber_review_guard] DataTaskIR 口径最终检视"
        f"（第 {fires}/{max_fires} 次），转换为 request_human_feedback"
    )
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
    return bool(names & _NL2SQL_TOOLS)


def _caliber_review_enabled(runtime: Runtime | None) -> bool:
    if not _ir_hitl_enabled(runtime):
        return False
    raw = _data_task_ir_option(runtime, "enable_ir_caliber_review", False)
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(raw)


def _ir_hitl_enabled(runtime: Runtime | None) -> bool:
    raw = _data_task_ir_option(runtime, "enable_ir_hitl", True)
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    return bool(raw)


def _max_fires(runtime: Runtime | None) -> int:
    raw = _data_task_ir_option(runtime, "hitl_caliber_review_max_fires", _DEFAULT_MAX_FIRES)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_FIRES
    return max(1, value)


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


def _is_done(runtime: Runtime | None) -> bool:
    return bool(_cache_get(runtime, _REVIEW_DONE_KEY))


def _mark_done(runtime: Runtime | None, reason: str) -> None:
    if _is_done(runtime):
        return
    _cache_set(runtime, _REVIEW_DONE_KEY, True)
    logger.info(f"[ir_caliber_review_guard] done ({reason})")


def _fire_count(runtime: Runtime | None) -> int:
    try:
        return int(_cache_get(runtime, _REVIEW_FIRE_COUNT_KEY, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _pending_blocking_queue(runtime: Runtime | None) -> bool:
    queue = _cache_get(runtime, _IR_HITL_QUEUE_KEY, []) or []
    return bool(isinstance(queue, list) and queue)


def _allow_review_despite_pending_queue(runtime: Runtime | None) -> bool:
    """True when blocking asks will no longer be rewritten by ir_question_guard.

    Matches ensure_ir_ready best-effort release: either ``ir_hitl_best_effort`` is
    already set, or ``ir_question_guard.fire_count`` has reached
    ``DATA_TASK_IR.hitl_question_guard_max_fires``. Without this, a non-empty
    queue after the question-guard fire cap skips caliber review while SQL is
    still allowed to proceed.
    """
    if bool(_cache_get(runtime, _IR_HITL_BEST_EFFORT_KEY)):
        return True
    return _question_guard_fires_exhausted(runtime)


def _question_guard_max_fires(runtime: Runtime | None) -> int:
    raw = _data_task_ir_option(
        runtime, "hitl_question_guard_max_fires", _DEFAULT_QUESTION_GUARD_MAX_FIRES
    )
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_QUESTION_GUARD_MAX_FIRES
    return max(1, value)


def _question_guard_fires_exhausted(runtime: Runtime | None) -> bool:
    try:
        fires = int(_cache_get(runtime, _QUESTION_GUARD_FIRE_COUNT_KEY, 0) or 0)
    except (TypeError, ValueError):
        fires = 0
    return fires >= _question_guard_max_fires(runtime)


def _field_values(runtime: Runtime | None) -> list[dict[str, Any]]:
    raw = _cache_get(runtime, _IR_FIELD_VALUES_KEY, []) or []
    return raw if isinstance(raw, list) else []


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


def _maybe_complete_after_reply(runtime: Runtime | None, state: Mapping[str, Any]) -> None:
    if not _cache_get(runtime, _REVIEW_ASKED_KEY):
        return
    ask_digest = _cache_get(runtime, _REVIEW_ASK_DIGEST_KEY)
    if ask_digest is None:
        return
    if _feedback_digest(state) != ask_digest:
        _mark_done(runtime, "user replied after caliber review ask")


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
    tool_call = {
        "id": f"call_ir_review_{uuid.uuid4().hex[:12]}",
        "name": HUMAN_FEEDBACK_TOOL_NAME,
        "args": {
            "reason": (
                "DataTaskIR 口径最终检视：生成 SQL 前请确认已确认口径是否完整，"
                "或补充遗漏的过滤/窗口等条件。"
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
