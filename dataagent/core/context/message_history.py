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
"""Shared ``messages.json`` wire format and file I/O for LangChain message histories.

Provides serialize/deserialize, replay-safe sanitization, and explicit-path read/write
helpers. Flex session hooks and swarm worker persistence both reuse this module so
message artifacts stay format-compatible across session and worker directories.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage


def _serialize(msg: BaseMessage) -> dict[str, Any]:
    """Convert one LangChain message to the ``messages.json`` record wire format."""
    payload: dict[str, Any] = {
        "type": msg.__class__.__name__,
        "content": getattr(msg, "content", ""),
        "name": getattr(msg, "name", "") or "",
        "additional_kwargs": dict(getattr(msg, "additional_kwargs", {}) or {}),
        "response_metadata": dict(getattr(msg, "response_metadata", {}) or {}),
    }
    if isinstance(msg, AIMessage):
        payload["tool_calls"] = list(getattr(msg, "tool_calls", []) or [])
        payload["invalid_tool_calls"] = list(getattr(msg, "invalid_tool_calls", []) or [])
    if isinstance(msg, ToolMessage):
        tool_call_id = getattr(msg, "tool_call_id", "")
        payload["tool_call_id"] = str(tool_call_id) if tool_call_id is not None else ""
    return payload


def _deserialize(payload: dict[str, Any]) -> BaseMessage | None:
    """Parse one wire-format record into a LangChain message, or ``None`` when invalid."""
    t = str(payload.get("type", ""))
    content = payload.get("content", "")
    akw = payload.get("additional_kwargs") or {}
    rmeta = payload.get("response_metadata") or {}
    if not isinstance(akw, dict):
        akw = {}
    if not isinstance(rmeta, dict):
        rmeta = {}

    if t == "HumanMessage":
        return HumanMessage(content=content, additional_kwargs=akw, response_metadata=rmeta)
    if t == "AIMessage":
        tool_calls = payload.get("tool_calls") or []
        if not tool_calls and isinstance(akw.get("tool_calls"), list):
            tool_calls = akw.get("tool_calls") or []
        return AIMessage(
            content=content,
            additional_kwargs=akw,
            response_metadata=rmeta,
            tool_calls=tool_calls,
            invalid_tool_calls=payload.get("invalid_tool_calls") or [],
        )
    if t == "ToolMessage":
        tid = payload.get("tool_call_id", "")
        tid = tid if isinstance(tid, str) else ""
        if not tid.strip():
            return None
        return ToolMessage(
            content=content,
            tool_call_id=tid,
            additional_kwargs=akw,
            response_metadata=rmeta,
        )
    return None


def _valid_tool_call_ids(msg: AIMessage) -> set[str]:
    """Collect non-empty tool-call ids from an ``AIMessage`` payload."""
    calls = list(getattr(msg, "tool_calls", []) or [])
    if not calls:
        raw = getattr(msg, "additional_kwargs", {}).get("tool_calls", [])
        if isinstance(raw, list):
            calls = raw
    ids: set[str] = set()
    for c in calls:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if cid and str(cid).strip():
            ids.add(str(cid).strip())
    return ids


def _sanitize(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Drop orphan AIMessage→ToolMessage pairs (aligned with galatea replay sanitization)."""
    sanitized: list[BaseMessage] = []
    pending_ai: AIMessage | None = None
    pending_tools: list[ToolMessage] = []
    pending_ids: set[str] = set()

    def _flush(*, keep: bool) -> None:
        nonlocal pending_ai, pending_tools, pending_ids
        if keep and pending_ai is not None:
            sanitized.append(pending_ai)
            sanitized.extend(pending_tools)
        pending_ai = None
        pending_tools = []
        pending_ids = set()

    for msg in messages:
        if isinstance(msg, AIMessage):
            _flush(keep=not pending_ids)
            pending_ids = _valid_tool_call_ids(msg)
            if pending_ids:
                pending_ai = msg
                pending_tools = []
            else:
                sanitized.append(msg)
            continue
        if isinstance(msg, ToolMessage):
            tid = str(getattr(msg, "tool_call_id", "") or "").strip()
            if not pending_ids or tid not in pending_ids:
                continue
            pending_tools.append(msg)
            pending_ids.discard(tid)
            if not pending_ids:
                _flush(keep=True)
            continue
        _flush(keep=not pending_ids)
        sanitized.append(msg)

    _flush(keep=not pending_ids)
    return sanitized


def _read_raw(path: Path) -> list[dict[str, Any]]:
    """Load raw message records from a ``messages.json`` file."""
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload.get("messages", [])
        return records if isinstance(records, list) else []
    except (OSError, ValueError):
        return []


def _write_raw(path: Path, records: list[dict[str, Any]]) -> None:
    """Atomically write raw message records to a ``messages.json`` file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"messages": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def serialize_message(msg: BaseMessage) -> dict[str, Any]:
    """Serialize one LangChain message using the shared history wire format."""
    return _serialize(msg)


def deserialize_message(payload: dict[str, Any]) -> BaseMessage | None:
    """Deserialize one record from the shared history wire format."""
    return _deserialize(payload)


def sanitize_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Return messages safe for replay, filtering SystemMessage and orphan tool calls."""
    return _sanitize([m for m in messages if not isinstance(m, SystemMessage)])


def read_messages_file(path: Path | str) -> list[BaseMessage]:
    """Read and sanitize messages from an explicit ``messages.json`` path."""
    records = _read_raw(Path(path))
    messages: list[BaseMessage] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        msg = deserialize_message(record)
        if msg is not None:
            messages.append(msg)
    return sanitize_messages(messages)


def write_messages_file(path: Path | str, messages: list[BaseMessage]) -> None:
    """Write messages to an explicit ``messages.json`` path."""
    records = [serialize_message(m) for m in sanitize_messages(messages)]
    _write_raw(Path(path), records)


def write_message_records_file(path: Path | str, records: list[dict[str, Any]]) -> None:
    """Write already-serialized message records after deserialize/sanitize validation."""
    messages: list[BaseMessage] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        msg = deserialize_message(record)
        if msg is not None:
            messages.append(msg)
    write_messages_file(path, messages)
