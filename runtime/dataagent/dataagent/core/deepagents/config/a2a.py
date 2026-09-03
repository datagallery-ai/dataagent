"""Compile legacy A2A declarations into native asynchronous LangChain tools."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from a2a.client import create_client
from a2a.client.card_resolver import A2ACardResolver
from a2a.client.client import ClientConfig
from a2a.helpers import new_text_message
from a2a.types.a2a_pb2 import AgentCard, AgentSkill, Role, SendMessageRequest, TaskState
from langchain_core.tools import BaseTool, StructuredTool, ToolException

from dataagent.core.deepagents.config.tool_hooks import tag_tool


@dataclass(frozen=True)
class _A2AConnection:
    """Resolved connection settings for one remote A2A agent."""

    agent_id: str
    base_url: str
    auth_token: str | None
    timeout: float


class A2AToolConfigCompiler:
    """Compile ``TOOLS.A2A`` AgentCard skills into native LangChain tools."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = config

    @staticmethod
    def _as_mapping(value: Any, path: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{path} must be a mapping.")
        return value

    @staticmethod
    def _as_timeout(value: Any, path: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{path} must be a positive number.")
        return float(value)

    @staticmethod
    def _extract_http_status(exc: BaseException) -> tuple[int, str] | None:
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            response = getattr(current, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code is None:
                status_code = getattr(current, "status_code", None)
            if isinstance(status_code, int):
                reason = str(getattr(response, "reason_phrase", "") or "").strip()
                return status_code, reason
            current = current.__cause__ or current.__context__
        return None

    @staticmethod
    def _failed_task_message(status: Any, connection: _A2AConnection, skill_id: str) -> str | None:
        if int(getattr(status, "state", 0) or 0) != int(TaskState.TASK_STATE_FAILED):
            return None
        message = getattr(status, "message", None)
        parts = getattr(message, "parts", None) or []
        detail = "".join(str(part.text) for part in parts if getattr(part, "text", None)).strip()
        return detail or f"A2A agent '{connection.agent_id}' failed while handling skill '{skill_id}'."

    async def compile(self) -> tuple[BaseTool, ...]:
        """Discover all configured AgentCards and expose their skills as tools."""
        connections = self._compile_connections()
        if not connections:
            return ()
        discovered = await asyncio.gather(*(self._discover_tools(connection) for connection in connections))
        tools = tuple(tool for agent_tools in discovered for tool in agent_tools)
        self._validate_unique_tool_names(tools)
        return tools

    def _compile_connections(self) -> tuple[_A2AConnection, ...]:
        tools_config = self._tools_config()
        raw_entries = tools_config.get("A2A", [])
        if raw_entries is None:
            return ()
        if isinstance(raw_entries, (str, bytes)) or not isinstance(raw_entries, Sequence):
            raise ValueError("TOOLS.A2A must be a list.")

        connections: list[_A2AConnection] = []
        agent_ids: set[str] = set()
        for index, raw_entry in enumerate(raw_entries):
            path = f"TOOLS.A2A[{index}]"
            entry = self._as_mapping(raw_entry, path)
            agent_id, agent_config = self._unpack_entry(entry, path)
            if agent_id in agent_ids:
                raise ValueError(f"Duplicate A2A agent id: {agent_id!r}.")
            agent_ids.add(agent_id)
            connections.append(self._compile_connection(agent_id, agent_config, path))
        return tuple(connections)

    def _tools_config(self) -> Mapping[str, Any]:
        raw = self._config.get("TOOLS", {})
        if raw is None:
            return {}
        return self._as_mapping(raw, "TOOLS")

    def _unpack_entry(self, entry: Mapping[str, Any], path: str) -> tuple[str, Mapping[str, Any]]:
        direct_agent_id = str(entry.get("agent_id") or entry.get("name") or "").strip()
        if direct_agent_id:
            return direct_agent_id, entry
        if len(entry) != 1:
            raise ValueError(f"{path} must contain one agent id key or an agent_id field.")
        agent_id, raw_config = next(iter(entry.items()))
        normalized_agent_id = str(agent_id).strip()
        if not normalized_agent_id:
            raise ValueError(f"{path} agent id must be non-empty.")
        return normalized_agent_id, self._as_mapping(raw_config, f"{path}.{normalized_agent_id}")

    def _compile_connection(
        self,
        agent_id: str,
        agent_config: Mapping[str, Any],
        path: str,
    ) -> _A2AConnection:
        base_url = str(agent_config.get("base_url") or "").strip().rstrip("/")
        if not base_url:
            raise ValueError(f"{path}.{agent_id}.base_url is required.")
        auth_token = str(agent_config.get("auth_token") or "").strip() or None
        timeout = self._as_timeout(agent_config.get("timeout", 30), f"{path}.{agent_id}.timeout")
        return _A2AConnection(agent_id=agent_id, base_url=base_url, auth_token=auth_token, timeout=timeout)

    async def _discover_tools(self, connection: _A2AConnection) -> tuple[BaseTool, ...]:
        agent_card = await self._get_agent_card(connection)
        return tuple(self._create_tool(connection, agent_card, skill) for skill in agent_card.skills)

    async def _get_agent_card(self, connection: _A2AConnection) -> AgentCard:
        headers = self._auth_headers(connection)
        try:
            async with httpx.AsyncClient(headers=headers, timeout=connection.timeout) as http_client:
                resolver = A2ACardResolver(httpx_client=http_client, base_url=connection.base_url)
                return await resolver.get_agent_card()
        except Exception as exc:
            raise ValueError(self._format_error(connection, "AgentCard discovery", exc)) from exc

    def _create_tool(
        self,
        connection: _A2AConnection,
        agent_card: AgentCard,
        skill: AgentSkill,
    ) -> BaseTool:
        skill_id = str(skill.id).strip()
        if not skill_id:
            raise ValueError(f"A2A agent '{connection.agent_id}' published a skill without an id.")

        async def call_a2a_skill(message: str) -> str:
            try:
                return await self._send_message(connection, agent_card, skill_id, message)
            except ToolException:
                raise
            except Exception as exc:
                raise ToolException(self._format_error(connection, f"skill '{skill_id}'", exc)) from exc

        description = str(skill.description or skill.name or f"A2A skill {skill_id}").strip()
        args_schema = {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": f"Natural language message to send to A2A skill '{skill_id}'.",
                }
            },
            "required": ["message"],
        }
        tool = StructuredTool.from_function(
            coroutine=call_a2a_skill,
            name=skill_id,
            description=description,
            args_schema=args_schema,
            handle_tool_error=True,
            metadata={"tool_type": "a2a", "agent_id": connection.agent_id, "remote_skill_id": skill_id},
        )
        return tag_tool(tool, "a2a", connection.agent_id)

    async def _send_message(
        self,
        connection: _A2AConnection,
        agent_card: AgentCard,
        skill_id: str,
        message_text: str,
    ) -> str:
        headers = self._auth_headers(connection)
        async with httpx.AsyncClient(headers=headers, timeout=connection.timeout) as http_client:
            client_config = ClientConfig(httpx_client=http_client, streaming=False)
            client = await create_client(agent=agent_card, client_config=client_config)
            request = SendMessageRequest(message=new_text_message(text=message_text, role=Role.ROLE_USER))
            async with client:
                responses = client.send_message(request)
                return await self._extract_result(responses, connection, skill_id)

    async def _extract_result(
        self,
        responses: AsyncIterator[Any],
        connection: _A2AConnection,
        skill_id: str,
    ) -> str:
        result_parts: list[str] = []
        failed_message: str | None = None
        async for response in responses:
            if response.HasField("task") and response.task.HasField("status"):
                failed_message = self._failed_task_message(response.task.status, connection, skill_id) or failed_message
            if response.HasField("status_update") and response.status_update.HasField("status"):
                failed_message = (
                    self._failed_task_message(response.status_update.status, connection, skill_id) or failed_message
                )
            if response.HasField("task") and response.task.artifacts:
                self._append_artifact_text(result_parts, response.task.artifacts)
            elif response.HasField("artifact_update") and response.artifact_update.HasField("artifact"):
                self._append_artifact_text(result_parts, (response.artifact_update.artifact,))
            elif response.HasField("message"):
                result_parts.extend(str(part.text) for part in response.message.parts if part.text)
        if failed_message is not None:
            raise ToolException(failed_message)
        return "".join(result_parts)

    def _format_error(self, connection: _A2AConnection, operation: str, exc: BaseException) -> str:
        http_status = self._extract_http_status(exc)
        if http_status is not None and http_status[0] in {401, 403}:
            status_code, reason = http_status
            status_text = f"HTTP {status_code} {reason}".strip()
            return (
                f"A2A authentication failed for agent '{connection.agent_id}' during {operation}: "
                f"the remote service rejected the configured Bearer token ({status_text}). "
                "Check TOOLS.A2A auth_token."
            )
        return f"A2A agent '{connection.agent_id}' failed during {operation}: {exc}"

    @staticmethod
    def _auth_headers(connection: _A2AConnection) -> dict[str, str]:
        if connection.auth_token is None:
            return {}
        return {"Authorization": f"Bearer {connection.auth_token}"}

    @staticmethod
    def _append_artifact_text(result_parts: list[str], artifacts: Sequence[Any]) -> None:
        for artifact in artifacts:
            result_parts.extend(str(part.text) for part in artifact.parts if part.text)

    @staticmethod
    def _validate_unique_tool_names(tools: Sequence[BaseTool]) -> None:
        names: set[str] = set()
        for tool in tools:
            if tool.name in names:
                raise ValueError(f"Duplicate A2A skill id: {tool.name!r}.")
            names.add(tool.name)
