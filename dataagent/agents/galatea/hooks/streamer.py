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
from __future__ import annotations

import ast
import json
from typing import Any

from dataagent.agents.galatea.state.state import State
from dataagent.core.cbb.runtime import Runtime


def _json_loads_dict_or_empty(s: str) -> dict[str, Any] | None:
    """Parse JSON; return dict or ``{}`` if value is not an object; ``None`` if not valid JSON."""
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else {}


def _content_to_text(content: Any) -> str:
    """Convert content to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                chunks.append(text if isinstance(text, str) else str(item))
            else:
                chunks.append(str(item))
        return "\n".join(chunks).strip()
    return str(content)


def _parse_tool_payload(content: Any) -> dict[str, Any]:
    """Parse tool payload."""
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        return {}
    stripped = content.strip()
    if not stripped:
        return {}
    from_json = _json_loads_dict_or_empty(stripped)
    if from_json is not None:
        return from_json
    try:
        parsed = ast.literal_eval(stripped)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, SyntaxError):
        return {}


def streamer(state: State, runtime: Runtime) -> State:
    """Streamer."""
    sink = getattr(runtime.env, "event_sink", None)
    if not callable(sink):
        return state

    cur = runtime.stream
    cur.bootstrap(state.get("_stream_emitted_from", 0))
    messages = state.get("messages", [])
    if cur.emitted >= len(messages):
        return state

    for message in messages[cur.emitted :]:
        cls_name = message.__class__.__name__.lower()
        if "human" in cls_name or "system" in cls_name:
            continue

        if "tool" in cls_name:
            role = "tool"
            tool_payload = _parse_tool_payload(getattr(message, "content", ""))
            status = str(tool_payload.get("status", "")).upper()
            label = str(tool_payload.get("tool_name", "") or getattr(message, "name", "") or "")
            if status == "ERROR":
                content = _content_to_text(tool_payload.get("error", "")).strip()
            else:
                content = _content_to_text(tool_payload.get("result", "")).strip()
            if not content:
                content = _content_to_text(getattr(message, "content", "")).strip()
        elif "ai" in cls_name:
            role = "ai"
            content = _content_to_text(getattr(message, "content", "")).strip()
            status = ""
            label = str(getattr(message, "name", "") or "")
        else:
            role = "agent"
            content = _content_to_text(getattr(message, "content", "")).strip()
            status = ""
            label = str(getattr(message, "name", "") or "")

        if content:
            step_id = cur.take_step_id()
            sink(
                {
                    "type": "step",
                    "step": {"id": str(step_id), "role": role, "label": label, "status": status, "content": content},
                }
            )

        tool_calls = getattr(message, "tool_calls", None) or []
        for call in tool_calls:
            args = call.get("args", {})
            if isinstance(args, str):
                args_text = args
            else:
                try:
                    args_text = json.dumps(args, ensure_ascii=False)
                except (TypeError, ValueError):
                    args_text = str(args)
            step_id = cur.take_step_id()
            sink(
                {
                    "type": "step",
                    "step": {
                        "id": str(step_id),
                        "role": "tool_call",
                        "label": str(call.get("name", "tool_call")),
                        "content": args_text,
                    },
                }
            )

    cur.advance_to(len(messages))
    return state
