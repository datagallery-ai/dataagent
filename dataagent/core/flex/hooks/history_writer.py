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

from pathlib import Path

from langchain_core.messages import BaseMessage

from dataagent.core.context.message_history import read_messages_file, write_messages_file
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
