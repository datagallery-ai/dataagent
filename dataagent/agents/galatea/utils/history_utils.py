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
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from dataagent.agents.galatea.utils.json_store import read_json_object, write_json_object
from dataagent.agents.galatea.utils.workspace_utils import workspace_state_dir


def _history_path(workspace_dir: Path) -> Path:
    """History path."""
    return workspace_state_dir(workspace_dir) / "messages.json"


def _serialize_message(message: BaseMessage) -> dict:
    """Serialize message."""
    payload = {
        "type": message.__class__.__name__,
        "content": getattr(message, "content", ""),
        "name": getattr(message, "name", "") or "",
        "additional_kwargs": dict(getattr(message, "additional_kwargs", {}) or {}),
        "response_metadata": dict(getattr(message, "response_metadata", {}) or {}),
    }
    if isinstance(message, AIMessage):
        payload["tool_calls"] = list(getattr(message, "tool_calls", []) or [])
        payload["invalid_tool_calls"] = list(getattr(message, "invalid_tool_calls", []) or [])
    if isinstance(message, ToolMessage):
        tool_call_id = getattr(message, "tool_call_id", "")
        payload["tool_call_id"] = str(tool_call_id) if tool_call_id is not None else ""
    return payload


def _deserialize_message(payload: dict) -> BaseMessage | None:
    """Deserialize message."""
    message_type = str(payload.get("type", ""))
    content = payload.get("content", "")
    additional_kwargs = payload.get("additional_kwargs", {}) or {}
    if not isinstance(additional_kwargs, dict):
        additional_kwargs = {}
    response_metadata = payload.get("response_metadata", {}) or {}
    if not isinstance(response_metadata, dict):
        response_metadata = {}
    if message_type == "SystemMessage":
        return SystemMessage(content=content, additional_kwargs=additional_kwargs, response_metadata=response_metadata)
    if message_type == "HumanMessage":
        return HumanMessage(content=content, additional_kwargs=additional_kwargs, response_metadata=response_metadata)
    if message_type == "AIMessage":
        tool_calls = payload.get("tool_calls", []) or []
        if not tool_calls and isinstance(additional_kwargs.get("tool_calls"), list):
            tool_calls = additional_kwargs.get("tool_calls", []) or []
        return AIMessage(
            content=content,
            additional_kwargs=additional_kwargs,
            response_metadata=response_metadata,
            tool_calls=tool_calls,
            invalid_tool_calls=payload.get("invalid_tool_calls", []) or [],
        )
    if message_type == "ToolMessage":
        raw_tool_call_id = payload.get("tool_call_id", "")
        tool_call_id = raw_tool_call_id if isinstance(raw_tool_call_id, str) else ""
        if not tool_call_id.strip():
            return None
        return ToolMessage(
            content=content,
            tool_call_id=tool_call_id,
            additional_kwargs=additional_kwargs,
            response_metadata=response_metadata,
        )
    return None


def _valid_tool_call_ids(message: AIMessage) -> set[str]:
    """Valid tool call IDs."""
    tool_calls = list(getattr(message, "tool_calls", []) or [])
    if not tool_calls:
        raw_calls = getattr(message, "additional_kwargs", {}).get("tool_calls", [])
        if isinstance(raw_calls, list):
            tool_calls = raw_calls
    valid_ids: set[str] = set()
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        call_id = call.get("id")
        if call_id is None:
            continue
        text = str(call_id).strip()
        if text:
            valid_ids.add(text)
    return valid_ids


def _sanitize_messages_for_replay(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Sanitize messages for replay."""
    sanitized: list[BaseMessage] = []
    pending_ai_message: AIMessage | None = None
    pending_tool_messages: list[ToolMessage] = []
    pending_tool_call_ids: set[str] = set()

    def flush_pending(*, keep: bool) -> None:
        nonlocal pending_ai_message, pending_tool_messages, pending_tool_call_ids
        if keep and pending_ai_message is not None:
            sanitized.append(pending_ai_message)
            sanitized.extend(pending_tool_messages)
        pending_ai_message = None
        pending_tool_messages = []
        pending_tool_call_ids = set()

    for message in messages:
        if isinstance(message, AIMessage):
            flush_pending(keep=not pending_tool_call_ids)
            pending_tool_call_ids = _valid_tool_call_ids(message)
            if pending_tool_call_ids:
                pending_ai_message = message
                pending_tool_messages = []
            else:
                sanitized.append(message)
            continue
        if isinstance(message, ToolMessage):
            tool_call_id = str(getattr(message, "tool_call_id", "") or "").strip()
            if not pending_tool_call_ids:
                continue
            if tool_call_id not in pending_tool_call_ids:
                continue
            pending_tool_messages.append(message)
            pending_tool_call_ids.discard(tool_call_id)
            if not pending_tool_call_ids:
                flush_pending(keep=True)
            continue
        flush_pending(keep=not pending_tool_call_ids)
        sanitized.append(message)

    flush_pending(keep=not pending_tool_call_ids)
    return sanitized


def load_history_messages(workspace_dir: Path) -> list[BaseMessage]:
    """Load history messages."""
    history_path = _history_path(workspace_dir)
    if not history_path.exists():
        return []
    try:
        payload = read_json_object(history_path, {"messages": []})
        records = payload.get("messages", [])
        result: list[BaseMessage] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            message = _deserialize_message(record)
            if message is None or isinstance(message, SystemMessage):
                continue
            result.append(message)
        return _sanitize_messages_for_replay(result)
    except Exception:
        return []


def append_history_messages(workspace_dir: Path, messages: list[BaseMessage]) -> None:
    """Append history messages."""
    history_path = _history_path(workspace_dir)
    payload = read_json_object(history_path, {"messages": []})
    payload.setdefault("messages", []).extend([_serialize_message(m) for m in messages])
    write_json_object(history_path, payload)
