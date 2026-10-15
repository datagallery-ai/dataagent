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
"""Planner post-hook that rescues final text questions from terminating the actor loop.

背景（v3.2 Q34-Q71 run，2026-09-17）：agent 在最终回复文本里向用户提问
（"请问您确认使用方案 A 吗？"）而不调用 request_human_feedback 工具——
planner 对无工具调用的消息置 complete=True，actor loop 随即结束，问题无人
回答、SQL 未写，直接产出到 NO_SQL（~12/38 题）。本 hook 在完成前检测
"结尾在等用户答复"的文本回复，把该提问转换为 request_human_feedback 工具
调用，交由既有 HITL 管线（hitl_auto_resolver，受 budget 约束）作答后回到
planner 继续推进。

仅在 HITL 与自动回复均启用（无人值守评测场景）时生效；交互式会话中用户
会在下一轮直接答复文本提问，无需转换。

注意：本文件具双重模块身份——运行期经 suite-root 单文件直载（hooks.yaml 条目
``hooks.text_question_guard.text_question_guard``），测试/工具经包路径 import。
禁止模块级 mutable 状态，不支持包内相对导入（见 suite hooks.yaml 头注释）。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import AIMessage
from loguru import logger

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.workflow.state import FlexState

HUMAN_FEEDBACK_TOOL_NAME = "request_human_feedback"
_GUARD_FIRE_COUNT_KEY = "text_question_guard.fire_count"
_MAX_GUARD_FIRES = 2
_MAX_PENDING_ACTION_CHARS = 2000

# 结尾"等待用户答复"的强标记（校准自 v3.2 run：死胡同回复以 请问/请确认/您倾向
# …吗？/…？ 收尾；正常交付说明以陈述句收尾，不命中）
_AWAITING_USER_RE = re.compile(
    r"(请问|请确认|请您|您确认|您倾向|您选择|您决定|是否同意|是否确认|是否需要|"
    r"确认一下|帮我确认|您能|告诉我|您指定|吗\s*[？?]|呢\s*[？?])"
)


def text_question_guard(
    state: FlexState,
    runtime: Runtime | None,
    *,
    original_state: FlexState | None = None,
) -> FlexState:
    """把"最终文本提问"转换为 HITL 工具调用，避免 actor loop 带着未决问题终止。"""
    result = state
    if not result.get("complete"):
        return result
    if not _is_unattended_hitl(result, original_state, runtime):
        return result
    if _guard_fires(runtime) >= _MAX_GUARD_FIRES:
        return result

    messages = result.get("messages")
    last_ai = _last_ai_message(messages)
    if last_ai is None or _tool_calls(last_ai):
        return result
    content = last_ai.content if isinstance(last_ai.content, str) else ""
    if not _ends_awaiting_user(content):
        return result

    question_text = content.strip()[:_MAX_PENDING_ACTION_CHARS]
    updated_last = _with_human_feedback_tool_call(last_ai, question_text)
    updated_messages = _replace_last_ai(messages, updated_last)
    if updated_messages is None:
        return result

    fires = _guard_fires(runtime) + 1
    logger.warning(
        f"[text_question_guard] 最终回复以等待用户答复收尾（第 {fires}/{_MAX_GUARD_FIRES} 次），"
        f"转换为 request_human_feedback 继续 HITL 管线: {question_text[-80:]!r}"
    )
    _set_guard_fires(runtime, fires)
    return {
        **result,
        "messages": updated_messages,
        "complete": False,
        "need_human_feedback": True,
        "__hitl_in_current_turn__": False,
    }


def _is_unattended_hitl(
    result: Mapping[str, Any],
    original_state: Mapping[str, Any] | None,
    runtime: Runtime | None,
) -> bool:
    """仅无人值守 HITL（enable_human_feedback + hitl_auto_resolve）场景启用。"""
    source = original_state if original_state is not None else result
    enabled = source.get("enable_human_feedback")
    if enabled is None and runtime is not None:
        get_all_config = getattr(runtime, "get_all_config", None)
        if callable(get_all_config):
            try:
                config = get_all_config()
                agent_config = config.get("AGENT_CONFIG", {}) if isinstance(config, Mapping) else {}
                enabled = agent_config.get("enable_human_feedback", False)
            except Exception:
                enabled = False
    if not bool(enabled):
        return False
    from dataagent.core.flex.hooks.hitl_common import is_auto_resolve_enabled

    return is_auto_resolve_enabled(dict(source), None, runtime)


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


def _tool_calls(message: AIMessage) -> list[Any]:
    return list(message.tool_calls or []) + list(message.invalid_tool_calls or [])


def _ends_awaiting_user(content: str) -> bool:
    """判定回复是否以"等待用户答复"收尾。

    规则（校准自 v3.2 run 的 NO_SQL/PASS/FAIL 最终回复对比）：
    最后一个非空行需含强提问标记，且以问号/吗/呢收尾或含 请问/请确认/请您——
    交付说明（"…已修正为 X。"）不命中。
    """
    if not content:
        return False
    lines = [line for line in content.strip().splitlines() if line.strip()]
    if not lines:
        return False
    last = lines[-1].rstrip()
    if not _AWAITING_USER_RE.search(last):
        return False
    ends_question = last.endswith(("？", "?", "吗", "呢"))
    has_explicit_ask = any(kw in last for kw in ("请问", "请确认", "请您"))
    return ends_question or has_explicit_ask


def _replace_last_ai(messages: Any, updated: AIMessage) -> list[Any] | None:
    # 直接给单条 AIMessage（非列表）。裸消息形态返回 [updated]，由
    # BaseNode.aprocess 归一化合并（[*state.messages, *result_msgs]）。
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


def _with_human_feedback_tool_call(message: AIMessage, question_text: str) -> AIMessage:
    tool_call = {
        "id": f"call_text_guard_{uuid.uuid4().hex[:12]}",
        "name": HUMAN_FEEDBACK_TOOL_NAME,
        "args": {
            "reason": "该问题此前在普通回复文本中提出、未走确认工具，现转入正式确认流程。",
            "pending_action": question_text,
        },
    }
    return AIMessage(
        content=message.content,
        additional_kwargs=dict(message.additional_kwargs or {}),
        response_metadata=dict(message.response_metadata or {}),
        id=message.id,
        name=message.name,
        tool_calls=[*list(message.tool_calls or []), tool_call],
        invalid_tool_calls=list(message.invalid_tool_calls or []),
        usage_metadata=message.usage_metadata,
    )
