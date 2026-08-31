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
"""Flex session message history — session-scoped persistence adapter.

写入路径：
- **router 增量写**：每个路由节点执行完后调用 ``save_messages``（不依赖 ``enable_portrait``），
  崩溃时已执行轮次的消息不丢失。（见 ``FlexRouter._write_message_history``）
- **全量覆写**（``save_messages``）：由 ``portraiter`` 在 workflow 结束时再写一遍，
  保证文件与最终 state 完全一致（``enable_portrait`` 仅控制是否再走 LLM 更新画像文件）。

读取时用 ``load_messages``，底层复用 ``dataagent.core.context.message_history`` 的清洗逻辑。

产物路径：``{workspace}/.memory/messages.json``（layout 可配）

``messages_full.json`` 为 node 级 audit 流水（write-only，不参与 session restore）。
写入策略与 ``messages.json`` 对齐：wrapped subagent（非 Job 路径）跳过；Job subagent
与主 agent 正常写入各自 workspace 下的 ``.memory/messages_full.json``。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage

from dataagent.core.context.message_history import read_messages_file, serialize_message, write_messages_file
from dataagent.utils.runtime_paths import resolve_flex_session_memory_dir


def resolve_history_persistence_context(
    state: Mapping[str, Any],
    runtime: Any = None,
) -> tuple[str | Path | None, Mapping[str, Any] | None]:
    """Resolve workspace and merged config for session history persistence."""
    workspace = state.get("workspace")
    config: Mapping[str, Any] | None = None
    if runtime is not None:
        get_all_config = getattr(runtime, "get_all_config", None)
        if callable(get_all_config):
            merged = get_all_config()
            if isinstance(merged, Mapping):
                config = merged
    return workspace, config


def _resolve_session_memory_dir(
    user_id: str,
    session_id: str,
    *,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Resolve the Flex session memory directory without creating it."""
    return resolve_flex_session_memory_dir(
        user_id=user_id,
        session_id=session_id,
        workspace=workspace,
        config=config,
    )


def _session_memory_dir(
    user_id: str,
    session_id: str,
    *,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Resolve and ensure the Flex session memory directory."""
    mem_dir = _resolve_session_memory_dir(
        user_id,
        session_id,
        workspace=workspace,
        config=config,
    )
    mem_dir.mkdir(parents=True, exist_ok=True)
    return mem_dir


def _history_path(
    user_id: str,
    session_id: str,
    *,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Resolve and ensure the Flex session ``messages.json`` path."""
    return _session_memory_dir(user_id, session_id, workspace=workspace, config=config) / "messages.json"


def save_messages(
    user_id: str,
    session_id: str,
    messages: list[BaseMessage],
    *,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> None:
    """全量覆写 session history（由 portraiter 在 session 结束时调用）。

    若目标文件已存在（如上一 run 的残留），直接覆写，不再生成带时间戳的归档文件。
    跨进程重启时由 :func:`load_messages` 读取最终 ``messages.json`` 恢复历史。

    写入时 ``sanitize=False``：保留完整的消息序列（包括 HITL 等孤儿
    AIMessage），以便 ``round_summaries`` 的 token 统计不丢数据。
    读取时 :func:`read_messages_file` 仍会做 replay-safe 清洗，不影响重放。
    """
    if not user_id or not session_id:
        return
    path = _history_path(user_id, session_id, workspace=workspace, config=config)
    write_messages_file(path, messages, sanitize=False)


def load_messages(
    user_id: str,
    session_id: str,
    *,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> list[BaseMessage]:
    """加载并清洗 session history，过滤 SystemMessage 和孤儿 tool call 对。"""
    path = (
        _resolve_session_memory_dir(
            user_id,
            session_id,
            workspace=workspace,
            config=config,
        )
        / "messages.json"
    )
    return read_messages_file(path)


def _full_path(
    user_id: str,
    session_id: str,
    *,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Resolve and ensure the Flex session ``messages_full.json`` path."""
    return _session_memory_dir(user_id, session_id, workspace=workspace, config=config) / "messages_full.json"


def save_messages_full_for_state(
    state: Mapping[str, Any],
    messages: list[BaseMessage],
    *,
    runtime: Any = None,
) -> None:
    """Resolve persistence context from ``state`` and append to ``messages_full.json``.

    Wrapped subagents (non-Job path) skip persistence, matching ``messages.json`` policy
    so parent session audit files are not polluted when tools share the parent workspace.
    """
    from dataagent.core.flex.hooks.agent_turn import should_skip_main_session_history

    if should_skip_main_session_history(state):
        return
    workspace, config = resolve_history_persistence_context(state, runtime)
    save_messages_full(
        str(state.get("user_id") or ""),
        str(state.get("session_id") or ""),
        messages,
        workspace=workspace,
        config=config,
    )


def save_messages_full(
    user_id: str,
    session_id: str,
    messages: list[BaseMessage],
    *,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> None:
    """增量追加 node 原始输出消息到 ``messages_full.json``。

    每次 node 执行后调用，读已有文件 → 尾部去重（跳过与已存尾部重叠的新消息前缀）
    → 原子写回。节点重放 / checkpoint replay 不会产生重复记录。

    生产路径应优先调用 :func:`save_messages_full_for_state`，以便按 subagent 策略 skip。
    """
    if not user_id or not session_id or not messages:
        return
    path = _full_path(user_id, session_id, workspace=workspace, config=config)
    existing_records: list[dict] = []
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
            existing_records = data.get("messages", []) if isinstance(data, dict) else []
        except (OSError, json.JSONDecodeError):
            existing_records = []
    new_records = [serialize_message(m) for m in messages]

    # 尾部去重：若待写入的前缀与已存文件尾部重叠，跳过重叠部分
    if existing_records and new_records:
        max_overlap = min(len(existing_records), len(new_records))
        for overlap in range(max_overlap, 0, -1):
            if existing_records[-overlap:] == new_records[:overlap]:
                new_records = new_records[overlap:]
                break

    if not new_records:
        return
    all_records = existing_records + new_records
    from dataagent.core.context.message_history import _compute_round_summaries

    round_summaries = _compute_round_summaries(all_records)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"messages": all_records, "round_summaries": round_summaries}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)
