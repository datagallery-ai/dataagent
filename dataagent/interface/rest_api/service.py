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

import asyncio
import hashlib
import re
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient, SemanticServiceError
from dataagent.agents.nl2sql.utils.prompt_files import (
    DEFAULT_EDITABLE_PROMPT,
    editable_prompt_path,
    read_prompt_file,
    write_prompt_file,
)
from dataagent.core.context.context import ContextFactory
from dataagent.interface.rest_api.start_service import ScenarioTable, UnknownScenarioError
from dataagent.interface.sdk.agent import DataAgent
from dataagent.utils.log import logger

_ANSWER_TAG_RE = re.compile(r"<answer>\s*(.*?)\s*</answer>", flags=re.DOTALL | re.IGNORECASE)
# Keep the semantic status string. ``completed`` is only a summary: updating and failed
# are both incomplete, and callers tell those two states apart by ``status``.
_INGESTION_COMPLETED = {
    "normal": True,
    "updating": False,
    "failed": False,
}
_SEMANTIC_NOT_UP = "semantic service is not UP"
_INGESTION_UNAVAILABLE = "failed to query ingestion status"


def _trace_status(payload: Any) -> str | None:
    """Return the single ingestion trace status from ``items[0]``."""
    if not isinstance(payload, dict):
        return None
    items = payload.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return None
    status = items[0].get("status")
    if status not in _INGESTION_COMPLETED:
        return None
    return str(status)


def _ingestion_result(status: str) -> dict[str, Any]:
    """One ingestion trace. ``status`` is the semantic value, not collapsed to ``completed``."""
    return {
        "result": {
            "success": True,
            "status": status,
            "completed": _INGESTION_COMPLETED[status],
        }
    }


def _downstream_json(exc: SemanticServiceError) -> Any | None:
    """Return the semantic-service JSON body when the HTTP error has one."""
    response = exc.response
    if response is None or not isinstance(exc.status_code, int):
        return None
    try:
        body = response.json()
    except Exception:
        return None
    if body is None or isinstance(body, (dict, list)):
        return body
    return None


def _downstream_text(body: dict[str, Any], *keys: str) -> str | None:
    """Return the first non-empty string among ``keys``."""
    for key in keys:
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


_PUBLIC_ERROR_FIELD_TYPES = {
    "code": str,
    "http_status": int,
    "component": str,
    "retryable": bool,
}


class DataAgentService:
    """DataAgent service facade."""

    def __init__(self, *, config_path: str | Path | None = None, table: ScenarioTable | None = None):
        """Initialize DataAgent service."""
        self._table = table
        if table is not None:
            self.config_path: Path | None = table.routes[table.default]
        else:
            self.config_path = Path(config_path).expanduser().resolve() if config_path is not None else None
        self._agents: dict[str, DataAgent] = {}
        self._agent: DataAgent | None = None
        self._cached_agent_type: str | None = None
        self._prompt_targets: dict[tuple[str, str], Path] = {}

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
    def _open_stream(agent: DataAgent, query: str, request: dict[str, Any]) -> AsyncIterator[Any]:
        """Open the agent stream, seeding session/workspace state from the request scope."""
        initial_state: dict[str, Any] = {"user_query": query}
        if not request:
            return agent.astream(initial_state=initial_state, stream_mode=["updates", "custom", "values"])
        initial_state["session_id"] = request.get("session_id")
        workspace = request.get("workspace")
        if workspace is not None:
            initial_state["workspace"] = workspace
        return agent.astream(
            initial_state=initial_state,
            session_id=request.get("session_id"),
            stream_mode=["updates", "custom", "values"],
        )

    @staticmethod
    def _unpack_stream_item(item: tuple[Any, ...]) -> tuple[Any, Any]:
        """Return (stream_mode, data) from a 2- or 3-tuple stream item."""
        if len(item) == 3:
            return item[1], item[2]
        return item[0], item[1]

    @staticmethod
    def _merge_update_state(data: Any, update_state: dict[str, Any]) -> None:
        """Merge the dict values of one ``updates`` chunk into update_state."""
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, dict):
                    update_state.update(value)

    @staticmethod
    def _normalize_sql_security_errors(error: dict[str, Any]) -> list[dict[str, str]]:
        """Keep only public SQL security codes and messages."""
        if error.get("component") != "sql_security":
            return []
        errors = error.get("errors")
        if not isinstance(errors, list):
            return []
        public_errors: list[dict[str, str]] = []
        for item in errors:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            message = item.get("message")
            if not isinstance(code, str) or not code.startswith("NL2SQL-SEC-"):
                continue
            if not isinstance(message, str) or not message.strip():
                continue
            public_errors.append({"code": code, "message": message})
        return public_errors

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

    @staticmethod
    def _load_agent(path: Path) -> DataAgent:
        """Load one DataAgent from a yaml path."""
        try:
            agent = DataAgent.from_config(path)
        except Exception as exc:
            raise RuntimeError("DataAgent.from_config raised an exception") from exc
        if agent is None:
            raise RuntimeError("DataAgent.from_config returned None")
        return agent

    @staticmethod
    def _semantic_get(
        agent: DataAgent,
        path: str,
        *,
        params: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """GET one semantic path using that agent's ``SEMANTIC_LAYER.base_url``."""
        client = SemanticServiceClient.from_config(agent.config)
        try:
            return client.get(path, params=params, headers=headers)
        finally:
            client.client.close()

    def initialize(self) -> None:
        """Initialize one DataAgent for the single config, or one per scenario."""
        if self._table is None:
            if self.config_path is None:
                raise ValueError("DataAgent service requires --config.")
            self._agent = self._load_agent(self.config_path)
        else:
            loaded = {name: self._load_agent(path) for name, path in self._table.routes.items()}
            self._agents = loaded
            self._agent = loaded[self._table.default]
        self._cached_agent_type = str(getattr(self._agent, "type", "") or "react")

    def resolve_scenario(self, scenario: str | None) -> str | None:
        """Return the registered scenario name. None keeps the single-config agent."""
        if self._table is None:
            if scenario:
                raise UnknownScenarioError(scenario)
            return None
        if not scenario:
            return self._table.default
        if scenario not in self._table.routes:
            raise UnknownScenarioError(scenario)
        return scenario

    def is_ready(self) -> bool:
        """Return True when the underlying agent has been initialized."""
        return self._agent is not None

    async def semantic_status(self) -> dict[str, Any]:
        """Report whether the default scenario's semantic service process is UP."""
        agent: DataAgent | None = None
        try:
            agent = self._default_agent()
            payload = await asyncio.to_thread(self._semantic_get, agent, "health")
        except SemanticServiceError as exc:
            logger.warning("semantic status failed: status={} error_code={}", exc.status_code, exc.error_code)
            return self._format_downstream_error(exc, fallback=_SEMANTIC_NOT_UP, agent=agent)
        except Exception as exc:
            logger.exception("semantic status failed: {}", {"type": exc.__class__.__name__})
            return self._format_error(_SEMANTIC_NOT_UP, agent=agent)
        if isinstance(payload, dict) and payload.get("status") == "UP":
            return {"result": {"success": True, "status": "UP"}}
        return self._format_downstream_body(payload, status_code=200, fallback=_SEMANTIC_NOT_UP, agent=agent)

    async def ingestion_status(self, scenario: str, trace_id: str) -> dict[str, Any]:
        """Report one ingestion job. ``normal`` is complete; ``updating`` and ``failed`` stay distinct."""
        agent: DataAgent | None = None
        try:
            agent = self._select(scenario)
            payload = await asyncio.to_thread(
                self._semantic_get,
                agent,
                "ingestion-traces",
                params={"traceId": trace_id},
            )
        except UnknownScenarioError:
            raise
        except SemanticServiceError as exc:
            logger.warning(
                "ingestion status failed: status={} error_code={}",
                exc.status_code,
                exc.error_code,
            )
            return self._format_downstream_error(exc, fallback=_INGESTION_UNAVAILABLE, agent=agent)
        except Exception as exc:
            logger.exception("ingestion status failed: {}", {"type": exc.__class__.__name__})
            return self._format_error(_INGESTION_UNAVAILABLE, agent=agent)
        status = _trace_status(payload)
        if status is None:
            return self._format_error(_INGESTION_UNAVAILABLE, agent=agent)
        return _ingestion_result(status)

    async def query(self, query: str, scenario: str | None = None) -> Any:
        """Run one DataAgent query."""
        agent: DataAgent | None = None
        try:
            agent = self._select(scenario)
            with self._request_scope(agent) as request:
                if not request:
                    return self._format_result(await agent.chat(query), agent=agent)
                initial_state = {"session_id": request.get("session_id")}
                return self._format_result(
                    await agent.chat(query, initial_state=initial_state, **request),
                    agent=agent,
                )
        except UnknownScenarioError:
            raise
        except Exception as exc:
            logger.exception(
                "Unexpected DataAgent query error: {}",
                {"message": str(exc), "type": exc.__class__.__name__},
            )
            return self._format_error("internal error", agent=agent)

    async def stream_query(self, query: str, scenario: str | None = None):
        """Stream one DataAgent query as message/result events."""
        agent: DataAgent | None = None
        try:
            agent = self._select(scenario)
            with self._request_scope(agent) as request:
                stream = self._open_stream(agent, query, request)
                async for event in self._stream_events(agent, stream):
                    yield event
        except UnknownScenarioError:
            raise
        except Exception as exc:
            logger.exception(
                "Unexpected DataAgent stream error: {}",
                {"message": str(exc), "type": exc.__class__.__name__},
            )
            yield {"event": "result", "data": self._format_error("internal error", agent=agent)}

    async def view_prompt(self, scenario: str, document: str = DEFAULT_EDITABLE_PROMPT) -> dict[str, Any]:
        """View one scenario's editable packaged prompt markdown and its last write time."""
        agent = self._agents.get(scenario) or self._agent
        try:
            path = self._prompt_target(scenario, document)
            markdown, updated_at = await asyncio.to_thread(read_prompt_file, path)
        except UnknownScenarioError:
            raise
        except Exception as exc:
            logger.exception(
                "Unexpected prompt view error: {}",
                {"message": str(exc), "type": exc.__class__.__name__},
            )
            return self._format_error(str(exc), agent=agent)
        return {"result": {"success": True, "markdown": markdown, "updated_at": updated_at}}

    async def update_prompt(
        self, scenario: str, markdown: str, document: str = DEFAULT_EDITABLE_PROMPT
    ) -> dict[str, Any]:
        """Replace one scenario's editable packaged prompt markdown; effective on the next query."""
        agent = self._agents.get(scenario) or self._agent
        try:
            path = self._prompt_target(scenario, document)
            updated_at = await asyncio.to_thread(write_prompt_file, path, markdown)
        except UnknownScenarioError:
            raise
        except Exception as exc:
            logger.exception(
                "Unexpected prompt update error: {}",
                {"message": str(exc), "type": exc.__class__.__name__},
            )
            return self._format_error(str(exc), agent=agent)
        return {"result": {"success": True, "updated_at": updated_at}}

    def _default_agent(self) -> DataAgent:
        """Return the agent for the first ``--config``. A bare path has no scenario table."""
        if self._agent is None:
            self.initialize()
        if self._agent is None:
            raise RuntimeError("DataAgent service is not initialized.")
        return self._agent

    def _select(self, scenario: str | None) -> DataAgent:
        """Return the agent for this request. An empty scenario uses the default agent."""
        name = self.resolve_scenario(scenario)
        if self._agent is None:
            self.initialize()
        if name is None:
            if self._agent is None:
                raise RuntimeError("DataAgent service is not initialized.")
            return self._agent
        agent = self._agents.get(name)
        if agent is None:
            raise RuntimeError("DataAgent service is not initialized.")
        return agent

    def _prompt_target(self, scenario: str, document: str) -> Path:
        """Resolve (scenario, document) to its packaged md path, cached per pair."""
        if not scenario:
            raise UnknownScenarioError(scenario)
        name = self.resolve_scenario(scenario)
        if name is None or self._table is None:
            raise UnknownScenarioError(scenario)
        cache_key = (name, document)
        if cache_key not in self._prompt_targets:
            self._prompt_targets[cache_key] = editable_prompt_path(document, self._table.routes[name])
        return self._prompt_targets[cache_key]

    def _format_result(self, state: Any, agent: DataAgent | None = None) -> dict[str, Any]:
        """Format final agent state for the REST API."""

        if not isinstance(state, dict):
            return self._format_error("Agent returned an invalid result", agent=agent)
        if isinstance(state.get("error"), dict):
            return self._normalize_error_payload(state["error"], agent=agent)
        if state.get("success") is False:
            message = state.get("message")
            message = message if isinstance(message, str) and message.strip() else "Agent failed"
            return self._format_error(message, agent=agent)
        if state.get("error"):
            return self._format_error("Agent failed", agent=agent)

        if self._agent_type(agent) == "nl2sql":
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
            return self._format_error("Agent returned an empty result", agent=agent)
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
            # Backward-compatible flat fields for existing clients.
            "sql": sql,
            "confidence": state.get("confidence"),
            "columns": state.get("columns"),
            "rows_preview": rows_preview,
            "session_id": state.get("session_id"),
        }
        if sql:
            payload["sql_fingerprint"] = hash_sql(sql)
        return payload

    def _format_error(self, message: str, agent: DataAgent | None = None) -> dict[str, Any]:
        """Format base agent error payload."""
        return {
            "result": {
                "success": False,
                "code": self._agent_error_code(agent),
                "message": message,
                "http_status": 500,
                "component": "agent",
                "retryable": False,
            }
        }

    def _format_downstream_error(
        self,
        exc: SemanticServiceError,
        *,
        fallback: str,
        agent: DataAgent | None,
    ) -> dict[str, Any]:
        """Keep the agent error envelope and fill it from a semantic HTTP error."""
        status_code = exc.status_code if isinstance(exc.status_code, int) else 500
        return self._format_downstream_body(
            _downstream_json(exc),
            status_code=status_code,
            fallback=fallback,
            agent=agent,
        )

    def _format_downstream_body(
        self,
        body: Any,
        *,
        status_code: int,
        fallback: str,
        agent: DataAgent | None,
    ) -> dict[str, Any]:
        """Keep the agent error envelope and fill code, message, and status from downstream."""
        payload = self._format_error(fallback, agent=agent)
        result = payload["result"]
        result["http_status"] = status_code
        if not isinstance(body, dict):
            return payload
        code = _downstream_text(body, "errorCode", "error_code")
        message = _downstream_text(body, "errorMessage", "error_message", "message", "error", "status")
        if code:
            result["code"] = code
        if message:
            result["message"] = message
        return payload

    def _normalize_error_payload(self, error: Any, agent: DataAgent | None = None) -> dict[str, Any]:
        """Normalize agent stream error payloads."""
        if isinstance(error, dict):
            message = error.get("message")
            payload = self._format_error(message if isinstance(message, str) else "Agent failed", agent=agent)
            for field, expected_type in _PUBLIC_ERROR_FIELD_TYPES.items():
                value = error.get(field)
                if isinstance(value, expected_type) and not (expected_type is int and isinstance(value, bool)):
                    payload["result"][field] = value
            security_errors = self._normalize_sql_security_errors(error)
            if security_errors:
                payload["result"]["errors"] = security_errors
            return payload
        return self._format_error(str(error), agent=agent)

    async def _stream_events(self, agent: DataAgent, stream: AsyncIterator[Any]) -> AsyncIterator[dict[str, Any]]:
        """Consume one agent stream: yield message events, then one final result event."""
        last_message: str | None = None
        final_state: Any = None
        update_state: dict[str, Any] = {}
        async for item in stream:
            if isinstance(item, dict) and "error" in item:
                yield {
                    "event": "result",
                    "data": self._normalize_error_payload(item.get("error"), agent=agent),
                }
                return

            if not isinstance(item, tuple) or len(item) not in (2, 3):
                message = str(item)
                if message and message != last_message:
                    yield {"event": "message", "data": {"message": message}}
                    last_message = message
                continue
            stream_mode, data = self._unpack_stream_item(item)

            if stream_mode == "values":
                final_state = data
                continue

            if stream_mode == "updates":
                self._merge_update_state(data, update_state)
                message = self._extract_stream_message(data)
            elif stream_mode == "custom":
                message = self._extract_custom_message(data)
            else:
                continue
            if message and message != last_message:
                yield {"event": "message", "data": {"message": message}}
                last_message = message

        result_state = final_state if final_state is not None else update_state
        if result_state:
            yield {"event": "result", "data": self._format_result(result_state, agent=agent)}
        else:
            yield {
                "event": "result",
                "data": self._format_error("Agent returned an empty stream result", agent=agent),
            }

    def _agent_error_code(self, agent: DataAgent | None = None) -> str:
        """Return the fallback agent error code."""
        return "WORKFLOW-AGENT-001" if self._agent_type(agent) == "nl2sql" else "REACT-AGENT-001"

    def _agent_type(self, agent: DataAgent | None = None) -> str:
        """Return the type of the agent that served this call."""
        if agent is not None:
            return str(getattr(agent, "type", "") or "react")
        if self._cached_agent_type is not None:
            return self._cached_agent_type
        return str(getattr(self._agent, "type", "") or "react")

    @contextmanager
    def _request_scope(self, agent: DataAgent | None = None) -> Iterator[dict[str, Any]]:
        """Create isolated resources for one stateless REST request and release them on exit."""
        selected = self._agent if agent is None else agent
        if self._agent_type(None if agent is None else selected) != "nl2sql":
            yield {}
            return

        agent_config = getattr(selected, "config", {})
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
