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


def _resolve_layout_config_from_runtime() -> Mapping[str, Any] | None:
    """Best-effort merged config from the active runtime ContextVar."""
    try:
        from dataagent.core.framework_adapters.runtime.context import get_current_runtime

        runtime = get_current_runtime()
        get_all_config = getattr(runtime, "get_all_config", None)
        if callable(get_all_config):
            config = get_all_config()
            if isinstance(config, Mapping):
                return config
    except Exception as exc:
        logger.debug("[_resolve_layout_config_from_runtime] skipped: {}", exc)
    return None


def is_job_workspace_subagent(state: Mapping[str, Any]) -> bool:
    """Return True when subagent persistence belongs under ``{parent_ws}/<subagents_dir>/{id}/``."""
    from dataagent.utils.env_utils import get_env
    from dataagent.utils.runtime_paths import FLEX_PERSISTENCE_ROOT_ENV, is_job_subagent_workspace

    if get_env(FLEX_PERSISTENCE_ROOT_ENV):
        return True
    workspace = state.get("workspace")
    config = _resolve_layout_config_from_runtime()
    return bool(workspace) and is_job_subagent_workspace(str(workspace), config=config)


def should_skip_main_session_history(state: Mapping[str, Any]) -> bool:
    """Return True when a subagent must not write the parent agent session history."""
    return is_subagent(state) and not is_job_workspace_subagent(state)


def session_history_restore(state: dict[str, Any], runtime: Any) -> dict[str, Any]:
    """若 ``messages`` 为空且有 ``user_id``/``session_id``，从 ``messages.json`` 全量恢复。

    非 Job 路径 subagent 不做历史恢复，避免污染主 agent 的会话上下文。
    """
    if should_skip_main_session_history(state) or state.get("messages"):
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
