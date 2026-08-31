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

import hashlib
import re
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dataagent.core.context.context import ContextFactory
from dataagent.core.errors import DataAgentError
from dataagent.interface.sdk.agent import DataAgent
from dataagent.utils.log import logger

_ANSWER_TAG_RE = re.compile(r"<answer>\s*(.*?)\s*</answer>", flags=re.DOTALL | re.IGNORECASE)


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

    @staticmethod
    def _collect_nl2sql_candidates(state: dict[str, Any], hash_sql) -> list[dict[str, Any]]:
        """Build candidate list from generation/execution results without prompt fields."""
        sources = (
            state.get("execution_results") or state.get("validation_results") or state.get("generation_results") or []
        )
        candidates: list[dict[str, Any]] = []
        for item in sources:
            if isinstance(item, dict):
                item_sql = str(item.get("sql") or "")
                idx = item.get("id", len(candidates))
                digest = item.get("sql_sha256") or (hash_sql(item_sql) if item_sql else "")
            else:
                item_sql = str(getattr(item, "sql", "") or "")
                idx = getattr(item, "id", len(candidates))
                digest = getattr(item, "sql_sha256", None) or (hash_sql(item_sql) if item_sql else "")
            if not item_sql:
                continue
            candidates.append({"index": idx, "sql": item_sql, "sql_sha256": digest})
        return candidates

    def initialize(self) -> None:
        """Initialize the service agent."""
        if self.config_path is None:
            raise ValueError("DataAgent service requires --config.")
        try:
            self._agent = DataAgent.from_config(self.config_path)
        except DataAgentError:
            raise
        except Exception as exc:
            raise RuntimeError("DataAgent.from_config raised an exception") from exc
        if self._agent is None:
            raise RuntimeError("DataAgent.from_config returned None")
        self._cached_agent_type = str(getattr(self._agent, "type", "") or "react")

    def is_ready(self) -> bool:
        """Return True when the underlying agent has been initialized."""
        return self._agent is not None

    async def query(self, query: str) -> Any:
        """Run one DataAgent query."""
        if self._agent is None:
            self.initialize()
        if self._agent is None:
            raise DataAgentError.from_exception(RuntimeError("DataAgent service is not initialized."), component="rest")
        with self._request_scope() as request:
            if not request:
                return self._format_result(await self._agent.chat(query))
            initial_state = {"session_id": request.get("session_id")}
            return self._format_result(await self._agent.chat(query, initial_state=initial_state, **request))

    async def stream_query(self, query: str):
        """Stream one DataAgent query as message/result events."""
        final_state: Any = None
        update_state: dict[str, Any] = {}
        last_message: str | None = None

        try:
            if self._agent is None:
                self.initialize()

            with self._request_scope() as request:
                if request:
                    initial_state = {"user_query": query, "session_id": request.get("session_id")}
                    workspace = request.get("workspace")
                    if workspace is not None:
                        initial_state["workspace"] = workspace
                    stream = self._agent.astream(
                        initial_state=initial_state,
                        session_id=request.get("session_id"),
                        stream_mode=["updates", "custom", "values"],
                    )
                else:
                    stream = self._agent.astream(
                        initial_state={"user_query": query}, stream_mode=["updates", "custom", "values"]
                    )
                async for item in stream:
                    if isinstance(item, dict) and "error" in item:
                        raise self._coerce_error(item.get("error"))

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
                raise DataAgentError.from_exception(
                    RuntimeError("Agent returned an empty stream result"),
                    component="rest",
                )
        except DataAgentError as exc:
            logger.exception(
                "REST stream failed source={} trace_id={}",
                exc.source,
                exc.trace_id,
            )
            yield {"event": "result", "data": {"result": exc.to_dict()}}
        except Exception as exc:
            error = DataAgentError.from_exception(exc, component="rest")
            logger.exception(
                "REST stream failed source={} trace_id={}",
                error.source,
                error.trace_id,
            )
            yield {"event": "result", "data": {"result": error.to_dict()}}

    def _format_result(self, state: Any) -> dict[str, Any]:
        """Format final agent state for the REST API."""
        if not isinstance(state, dict):
            raise DataAgentError.from_exception(RuntimeError("Agent returned an invalid result"), component="rest")

        if state.get("error") not in (None, "", {}):
            raise self._coerce_error(state.get("error"))
        if state.get("success") is False:
            message = state.get("message")
            message = message if isinstance(message, str) and message.strip() else "Agent failed"
            raise DataAgentError.from_exception(RuntimeError(message), component="rest")

        if self._agent_type() == "nl2sql":
            return {"result": self._format_nl2sql_result(state)}

        messages = state.get("messages", [])
        if isinstance(messages, list) and messages:
            last_msg = messages[-1]
            content = str(
                last_msg.get("content", "") if isinstance(last_msg, dict) else getattr(last_msg, "content", "")
            )
        else:
            content = ""

        if not content:
            raise DataAgentError.from_exception(RuntimeError("Agent returned an empty result"), component="rest")
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

    def _format_nl2sql_result(self, state: dict[str, Any]) -> dict[str, Any]:
        """Format NL2SQL final state as structured candidates (no prompts / raw model text)."""

        def hash_sql(sql: str) -> str:
            return hashlib.sha256(re.sub(r"\s+", " ", (sql or "").strip()).encode()).hexdigest()

        sql = str(state.get("sql") or "")
        rows_preview = state.get("rows_preview")
        message = "SQL generated."
        if rows_preview:
            message = "SQL generated and executed with preview rows."
        if not sql:
            message = "No executable SQL was generated."

        candidates = self._collect_nl2sql_candidates(state, hash_sql)
        if not candidates and sql:
            candidates = [
                {
                    "index": 0,
                    "sql": sql,
                    "sql_sha256": hash_sql(sql),
                }
            ]

        payload = {
            "success": True,
            "message": message,
            "candidates": candidates,
            "sql": sql,
            "confidence": state.get("confidence"),
            "columns": state.get("columns"),
            "rows_preview": rows_preview,
            "session_id": state.get("session_id"),
        }
        if sql:
            payload["sql_fingerprint"] = hash_sql(sql)
        return payload

    def _coerce_error(self, error: Any) -> DataAgentError:
        """Turn a leftover state/stream error payload into DataAgentError."""
        if isinstance(error, DataAgentError):
            return error
        if isinstance(error, dict) and error.get("source"):
            return DataAgentError.from_dict(error)
        fact = self._unstructured_error_fact(error)
        if not fact:
            return DataAgentError.from_exception(RuntimeError("Agent failed"), component="rest")
        component = "nl2sql" if self._agent_type() == "nl2sql" else "rest"
        return DataAgentError(source="tool", component=component, fact=fact)

    @staticmethod
    def _unstructured_error_fact(error: Any) -> str:
        """Extract a public fact from a string or unstructured error payload."""
        if isinstance(error, str):
            return error.strip()
        if isinstance(error, dict):
            for key in ("fact", "message", "error"):
                value = error.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _agent_type(self) -> str:
        """Return current SDK agent type."""
        if self._cached_agent_type is not None:
            return self._cached_agent_type
        return str(getattr(self._agent, "type", "") or "react")

    @contextmanager
    def _request_scope(self) -> Iterator[dict[str, Any]]:
        """Create isolated resources for one stateless REST request and release them on exit."""
        if self._agent_type() != "nl2sql":
            yield {}
            return

        agent_config = getattr(self._agent, "config", {})
        if hasattr(agent_config, "get_all") and callable(agent_config.get_all):
            config = agent_config.get_all() or {}
        elif isinstance(agent_config, Mapping):
            config = agent_config
        else:
            config = {}

        user_id = str(config.get("USER_ID", "anonymous") or "anonymous")
        run_id = int(config.get("RUN_ID", 0) or 0)
        sub_id = int(config.get("SUB_ID", 0) or 0)
        session_id = str(uuid.uuid4())
        workspace_config = config.get("WORKSPACE", {})
        persistent_workspace = workspace_config.get("path") if isinstance(workspace_config, Mapping) else None
        temporary_workspace = None
        request: dict[str, Any] = {"session_id": session_id}
        if not persistent_workspace:
            temporary_workspace = tempfile.TemporaryDirectory(prefix="dataagent-rest-")
            request["workspace"] = Path(temporary_workspace.name)

        try:
            yield request
        finally:
            released = ContextFactory.release_context(
                user_id=user_id, session_id=session_id, run_id=run_id, sub_id=sub_id
            )
            if released:
                logger.debug("Released {} REST Context instance(s) for session {}", released, session_id)
            if temporary_workspace is not None:
                temporary_workspace.cleanup()
