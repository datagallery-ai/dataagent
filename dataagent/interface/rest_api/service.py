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

import re
from pathlib import Path
from typing import Any

from dataagent.interface.sdk.agent import DataAgent

_ANSWER_TAG_RE = re.compile(r"<answer>\s*(.*?)\s*</answer>", flags=re.DOTALL | re.IGNORECASE)
_PUBLIC_ERROR_FIELD_TYPES = {
    "code": str,
    "http_status": int,
    "component": str,
    "retryable": bool,
}


class DataAgentService:
    """DataAgent service facade."""

    def __init__(self, *, config_path: str | Path | None = None):
        """Initialize DataAgent service."""
        self.config_path = Path(config_path).expanduser().resolve() if config_path is not None else None
        self._agent: DataAgent | None = None
        self._cached_agent_type: str | None = None

    @staticmethod
    def _extract_stream_message(data: Any) -> str | None:
        """Extract NL2SQL pseudo-stream message from update chunks."""
        if not isinstance(data, dict):
            return None
        message = data.get("stream_message")
        if message:
            return str(message)
        for value in data.values():
            if isinstance(value, dict) and value.get("stream_message"):
                return str(value["stream_message"])
        return None

    @staticmethod
    def _extract_custom_message(data: Any) -> str | None:
        """Extract ReAct streaming message from custom chunks."""
        if not isinstance(data, dict):
            return str(data)
        event_type = data.get("type")
        if event_type == "break":
            return None
        if event_type == "output_msg":
            return str(data.get("content") or "") or None
        if data.get("message"):
            return str(data["message"])
        if event_type == "tool_status":
            tool_name = str(data.get("tool_name") or "tool")
            status = str(data.get("status") or "")
            summary = str(data.get("summary") or "")
            msg = f"{tool_name}: {status}".strip(": ")
            return f"{msg} - {summary}" if summary else msg
        return str(data.get("summary") or data.get("content") or "") or None

    def initialize(self) -> None:
        """Initialize the service agent."""
        if self.config_path is None:
            raise ValueError("DataAgent service requires --config.")
        try:
            self._agent = DataAgent.from_config(self.config_path)
        except Exception as exc:
            raise RuntimeError("DataAgent.from_config raised an exception") from exc
        if self._agent is None:
            raise RuntimeError("DataAgent.from_config returned None")
        self._cached_agent_type = str(getattr(self._agent, "type", "") or "react")

    async def query(self, query: str) -> Any:
        """Run one DataAgent query."""
        try:
            if self._agent is None:
                self.initialize()
            if self._agent is None:
                return self._format_error("DataAgent service is not initialized.")
            return self._format_result(await self._agent.chat(query))
        except Exception as exc:
            return self._format_error(str(exc))

    async def stream_query(self, query: str):
        """Stream one DataAgent query as message/result events."""
        final_state: Any = None
        update_state: dict[str, Any] = {}
        last_message: str | None = None

        try:
            if self._agent is None:
                self.initialize()

            stream = self._agent.astream(
                initial_state={"user_query": query}, stream_mode=["updates", "custom", "values"]
            )
            async for item in stream:
                if isinstance(item, dict) and "error" in item:
                    yield {"event": "result", "data": self._normalize_error_payload(item["error"])}
                    return

                if isinstance(item, tuple) and len(item) == 3:
                    _, stream_mode, data = item
                elif isinstance(item, tuple) and len(item) == 2:
                    stream_mode, data = item
                else:
                    message = str(item)
                    if message and message != last_message:
                        yield {"event": "message", "data": {"message": message}}
                        last_message = message
                    continue

                if stream_mode == "values":
                    final_state = data
                    continue

                if stream_mode == "updates":
                    if isinstance(data, dict):
                        for value in data.values():
                            if isinstance(value, dict):
                                update_state.update(value)
                    message = self._extract_stream_message(data)
                    if message and message != last_message:
                        yield {"event": "message", "data": {"message": message}}
                        last_message = message
                    continue

                if stream_mode == "custom":
                    message = self._extract_custom_message(data)
                    if message and message != last_message:
                        yield {"event": "message", "data": {"message": message}}
                        last_message = message

            result_state = final_state if final_state is not None else update_state
            if result_state:
                yield {"event": "result", "data": self._format_result(result_state)}
            else:
                yield {"event": "result", "data": self._format_error("Agent returned an empty stream result")}
        except Exception as exc:
            yield {"event": "result", "data": self._format_error(str(exc))}

    def _format_result(self, state: Any) -> dict[str, Any]:
        """Format final agent state for the REST API."""

        if not isinstance(state, dict):
            return self._format_error("Agent returned an invalid result")
        if isinstance(state.get("error"), dict):
            return self._normalize_error_payload(state["error"])
        if state.get("success") is False:
            message = state.get("message")
            message = message if isinstance(message, str) and message.strip() else "Agent failed"
            return self._format_error(message)
        if state.get("error"):
            return self._format_error("Agent failed")

        if self._agent_type() == "nl2sql":
            sql = str(state.get("sql") or "")
            rows_preview = state.get("rows_preview")
            message = "SQL generated."
            if rows_preview:
                message = "SQL generated and executed with preview rows."
            if not sql:
                message = "No executable SQL was generated."
            return {
                "result": {
                    "success": True,
                    "message": message,
                    "sql": sql,
                    "confidence": state.get("confidence"),
                    "columns": state.get("columns"),
                    "rows_preview": rows_preview,
                    "session_id": state.get("session_id"),
                },
            }

        messages = state.get("messages", [])
        if isinstance(messages, list) and messages:
            last_msg = messages[-1]
            content = str(
                last_msg.get("content", "") if isinstance(last_msg, dict) else getattr(last_msg, "content", "")
            )
        else:
            content = ""

        if not content:
            return self._format_error("Agent returned an empty result")
        match = _ANSWER_TAG_RE.search(content)
        sql = match.group(1).strip() if match else ""
        payload = {
            "success": True,
            "message": content,
            "confidence": state.get("confidence"),
            "columns": state.get("columns"),
            "rows_preview": state.get("rows_preview"),
            "session_id": state.get("session_id"),
        }
        if sql:
            payload["sql"] = sql
        return {"result": payload}

    def _format_error(self, message: str) -> dict[str, Any]:
        """Format base agent error payload."""
        return {
            "result": {
                "success": False,
                "code": self._agent_error_code(),
                "message": message,
                "http_status": 500,
                "component": "agent",
                "retryable": False,
            }
        }

    def _normalize_error_payload(self, error: Any) -> dict[str, Any]:
        """Normalize agent stream error payloads."""
        if isinstance(error, dict):
            message = error.get("message")
            payload = self._format_error(message if isinstance(message, str) else "Agent failed")
            for field, expected_type in _PUBLIC_ERROR_FIELD_TYPES.items():
                value = error.get(field)
                if isinstance(value, expected_type) and not (expected_type is int and isinstance(value, bool)):
                    payload["result"][field] = value
            return payload
        return self._format_error(str(error))

    def _agent_error_code(self) -> str:
        """Return the fallback agent error code."""
        return "WORKFLOW-AGENT-001" if self._agent_type() == "nl2sql" else "REACT-AGENT-001"

    def _agent_type(self) -> str:
        """Return current SDK agent type."""
        if self._cached_agent_type is not None:
            return self._cached_agent_type
        return str(getattr(self._agent, "type", "") or "react")
