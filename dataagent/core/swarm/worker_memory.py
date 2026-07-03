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
"""负责 subagent worker 的state和消息历史的读存"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage

from dataagent.core.context.message_history import (
    deserialize_message,
    read_messages_file,
    sanitize_messages,
    write_messages_file,
)
from dataagent.core.swarm.worker_io import atomic_write_json
from dataagent.utils.runtime_paths import resolve_worker_memory_dir, resolve_worker_root

# 需要删除的运行时身份字段
SUBAGENT_DISK_STRIP_KEYS: frozenset[str] = frozenset(
    {"messages", "user_id", "session_id", "run_id", "sub_id", "user_query", "complete"}
)


def coerce_worker_messages(messages: list[BaseMessage] | list[dict[str, Any]]) -> list[BaseMessage]:
    """Normalize heterogeneous worker payloads into sanitized ``BaseMessage`` lists for ``messages.json``.

    Accepts LangChain messages or serialized dict records (as emitted over the child protocol).
    Applies the same ``message_history`` sanitization as session histories (no FIFO or truncation here).
    """
    if not messages:
        return []
    if isinstance(messages[0], dict):
        parsed: list[BaseMessage] = []
        for record in messages:
            if not isinstance(record, dict):
                continue
            msg = deserialize_message(record)
            if msg is not None:
                parsed.append(msg)
        return sanitize_messages(parsed)
    return sanitize_messages([m for m in messages if isinstance(m, BaseMessage)])


def strip_subagent_runtime_fields(state: dict[str, Any]) -> dict[str, Any]:
    """Remove volatile identity / transcript keys before persisting worker snapshots."""
    return {k: v for k, v in dict(state or {}).items() if k not in SUBAGENT_DISK_STRIP_KEYS}


def worker_has_persisted_assets(
    *,
    user_id: str,
    parent_session_id: str,
    sub_id: int,
    parent_workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> bool:
    """Return True when any reusable worker artifact exists on disk for ``sub_id``.

    Uses ``resolve_worker_root`` only (no ``mkdir``) so existence checks do not create
    empty ``workers/<sub_id>/.memory`` directories as a side effect.
    """
    memory_dir = (
        resolve_worker_root(
            user_id=user_id,
            parent_session_id=parent_session_id,
            sub_id=sub_id,
            parent_workspace=parent_workspace,
            config=config,
        )
        / ".memory"
    )
    return any((memory_dir / name).exists() for name in ("metadata.json", "messages.json", "subagent_state.json"))


def load_worker_subagent_state(
    *,
    user_id: str,
    parent_session_id: str,
    sub_id: int,
    parent_workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Load ``subagent_state.json`` for reuse, stripping runtime keys stored only in-memory."""
    memory_dir = resolve_worker_memory_dir(
        user_id=user_id,
        parent_session_id=parent_session_id,
        sub_id=sub_id,
        parent_workspace=parent_workspace,
        config=config,
    )
    path = memory_dir / "subagent_state.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return strip_subagent_runtime_fields(payload)


def load_worker_messages(
    user_id: str,
    parent_session_id: str,
    sub_id: int,
    *,
    parent_workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> list[BaseMessage] | None:
    """Load worker conversation history from ``messages.json``.

    Returns ``None`` when the file is missing or empty. Full transcripts are stored
    by overwrite on each successful parent persistence pass; compression/truncation
    follows the same subgraph paths as the main agent rather than a worker-local FIFO.
    """
    memory_dir = resolve_worker_memory_dir(
        user_id=user_id,
        parent_session_id=parent_session_id,
        sub_id=sub_id,
        parent_workspace=parent_workspace,
        config=config,
    )
    messages_path = memory_dir / "messages.json"
    if not messages_path.exists():
        return None
    messages = read_messages_file(messages_path)
    if not messages:
        return None
    return messages


def persist_worker_messages(
    *,
    user_id: str,
    parent_session_id: str,
    sub_id: int,
    messages: list[BaseMessage] | list[dict[str, Any]],
    parent_workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> None:
    """Persist the full multi-turn worker transcript, replacing ``messages.json`` in full."""
    memory_dir = resolve_worker_memory_dir(
        user_id=user_id,
        parent_session_id=parent_session_id,
        sub_id=sub_id,
        parent_workspace=parent_workspace,
        config=config,
    )
    path = memory_dir / "messages.json"
    normalized = coerce_worker_messages(messages)
    write_messages_file(path, normalized)


def persist_worker_state(
    *,
    user_id: str,
    parent_session_id: str,
    sub_id: int,
    state: dict[str, Any],
    parent_workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> None:
    """Write ``subagent_state.json`` with the child's serializable state minus ``messages``.

    Runtime identity keys are stripped so reuse never resurrects stale ``run_id`` /
    ``session_id`` values from disk.
    """
    memory_dir = resolve_worker_memory_dir(
        user_id=user_id,
        parent_session_id=parent_session_id,
        sub_id=sub_id,
        parent_workspace=parent_workspace,
        config=config,
    )
    cleaned = strip_subagent_runtime_fields(dict(state or {}))
    cleaned.pop("messages", None)
    atomic_write_json(memory_dir / "subagent_state.json", cleaned)
