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

产物路径：``~/.dataagent/{user_id}/{session_id}/.memory/messages.json``
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.messages import BaseMessage

from dataagent.core.context.message_history import read_messages_file, serialize_message, write_messages_file
from dataagent.utils.runtime_paths import resolve_session_root


def _history_path(user_id: str, session_id: str) -> Path:
    """Resolve and ensure the Flex session ``messages.json`` path."""
    mem_dir = resolve_session_root(user_id=user_id, session_id=session_id) / ".memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    return mem_dir / "messages.json"


def save_messages(user_id: str, session_id: str, messages: list[BaseMessage]) -> None:
    """全量覆写 session history（由 portraiter 在 session 结束时调用）。"""
    if not user_id or not session_id:
        return
    path = _history_path(user_id, session_id)
    write_messages_file(path, messages)


def load_messages(user_id: str, session_id: str) -> list[BaseMessage]:
    """加载并清洗 session history，过滤 SystemMessage 和孤儿 tool call 对。"""
    path = _history_path(user_id, session_id)
    return read_messages_file(path)


def _full_path(user_id: str, session_id: str) -> Path:
    """Resolve and ensure the Flex session ``messages_full.json`` path."""
    mem_dir = resolve_session_root(user_id=user_id, session_id=session_id) / ".memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    return mem_dir / "messages_full.json"


def save_messages_full(user_id: str, session_id: str, messages: list[BaseMessage]) -> None:
    """增量追加 node 原始输出消息到 ``messages_full.json``。

    每次 node 执行后调用，读已有文件 → 尾部去重（跳过与已存尾部重叠的新消息前缀）
    → 原子写回。节点重放 / checkpoint replay 不会产生重复记录。
    """
    if not user_id or not session_id or not messages:
        return
    path = _full_path(user_id, session_id)
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
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"messages": all_records}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
