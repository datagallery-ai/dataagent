# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Flex 内置 agent 级 pre-hook：会话历史恢复；用户侧模板 Human 由 Planner 写入（见 messages_utils）。

由 :class:`~dataagent.core.flex.agent.FlexAgent` 在 YAML ``HOOKS.agent.pre`` **之前** 默认注册，
便于单测替换 ``FlexAgent._builtin_agent_pre_hooks`` 或只 mock 本模块函数。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from loguru import logger


def is_subagent(state: Mapping[str, Any]) -> bool:
    """判断当前 state 是否属于子 agent（``sub_id != 0``）。"""
    return state.get("sub_id", 0) != 0


def session_history_restore(state: dict[str, Any], runtime: Any) -> dict[str, Any]:
    """若 ``messages`` 为空且有 ``user_id``/``session_id``，从 ``messages.json`` 全量恢复。

    subagent 不做历史恢复，避免污染主 agent 的会话上下文。
    """
    if is_subagent(state) or state.get("messages"):
        return state
    user_id = str(state.get("user_id") or "").strip()
    session_id = str(state.get("session_id") or "").strip()
    if not user_id or not session_id:
        return state
    try:
        from dataagent.core.flex.hooks.history_writer import (
            load_messages,
            resolve_history_persistence_context,
        )

        workspace, config = resolve_history_persistence_context(state, runtime)
        messages = load_messages(
            user_id,
            session_id,
            workspace=workspace,
            config=config,
        )
        if messages:
            state["messages"] = messages
            logger.debug(f"[session_history_restore] restored {len(messages)} messages ({session_id})")
    except Exception as e:
        logger.debug(f"[session_history_restore] skipped: {e}")
    return state
