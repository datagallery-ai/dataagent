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
"""DataAgent A2A 1.0 client — southbound adapter for consuming external A2A agents.

Northbound interface (unchanged):
  - AgentConfig dataclass
  - A2AClientWrapper (call_tool, ping, list_tools, discover_capabilities, close)
  - A2AToolWrapper (call, acall, get_schema)
  - A2AToolRegistry (register_agent, discover_tools, health_check, cleanup)
  - a2a_registry global singleton
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from a2a.client import create_client
from a2a.client.card_resolver import A2ACardResolver
from a2a.client.client import ClientConfig
from a2a.helpers import new_text_message
from a2a.types.a2a_pb2 import AgentCard, Role, SendMessageRequest
from dataagent.core.managers.action_manager.base import (
    BaseTool,
    ErrorType,
    ToolError,
    ToolResult,
    ToolType,
    classify_exception,
)
from dataagent.core.managers.action_manager.schemas import ParameterSchema, ToolSchema

A2A_AVAILABLE = True


def _classify_exception(exc: Exception) -> ErrorType:
    """根据异常类型分类错误（保持向后兼容，内部委托给统一函数）"""
    err_type, _ = classify_exception(exc)
    return err_type


@dataclass
class AgentConfig:
    """A2A agent configuration (northbound API unchanged)."""

    agent_id: str
    base_url: str
    auth_token: str | None = None
    timeout: int = 30
    category: str = "a2a"
    description: str = ""


class A2AClientWrapper:
    """DataAgent A2A client wrapper, based on a2a-sdk>=1.0.0."""

    def __init__(self, config: AgentConfig):
        if not A2A_AVAILABLE:
            raise ToolError("a2a-sdk is required for A2A tools. Install with: pip install a2a-sdk>=1.0.0")

        self.config = config
        self._agent_card: AgentCard | None = None

    @staticmethod
    async def _extract_result_text_from_stream(responses) -> str:
        """Extract result text from a2a-sdk 1.0 StreamResponse async iterator."""
        result_text = ""
        async for response in responses:
            # Task-level artifacts
            if response.HasField("task") and response.task.artifacts:
                for artifact in response.task.artifacts:
                    for part in artifact.parts:
                        if part.text:
                            result_text += part.text
            # Artifact update events
            elif response.HasField("artifact_update") and response.artifact_update.HasField("artifact"):
                for part in response.artifact_update.artifact.parts:
                    if part.text:
                        result_text += part.text
            # Message-level content (fallback)
            elif response.HasField("message") and response.message.parts:
                for part in response.message.parts:
                    if part.text:
                        result_text += part.text
        return result_text

    async def close(self):
        """Release resources (no persistent client in v1.0, no-op)."""
        pass

    async def call_tool(self, tool_name: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """Call a tool on the remote A2A agent via a2a-sdk 1.0."""
        # Build auth headers
        headers = {}
        if self.config.auth_token:
            headers["Authorization"] = f"Bearer {self.config.auth_token}"

        # Build the user message from the 'message' parameter (or fallback to JSON)
        message_text = parameters.get("message", json.dumps(parameters, ensure_ascii=False))

        message = new_text_message(text=message_text, role=Role.ROLE_USER)
        request = SendMessageRequest(message=message)

        timeout_seconds = self.config.timeout

        # Create a fresh client for each call to avoid connection state issues
        httpx_client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers=headers,
        )
        config = ClientConfig(httpx_client=httpx_client, streaming=False)

        try:
            client = await create_client(
                agent=self.config.base_url,
                client_config=config,
            )
            async with client:
                responses = client.send_message(request)
                result_text = await self._extract_result_text_from_stream(responses)
                return result_text if result_text else str(responses)
        except Exception as e:
            raise ToolError(f"Error during tool call '{tool_name}': {e}") from e
        finally:
            await httpx_client.aclose()

    async def ping(self) -> bool:
        """Check if the remote agent is online by fetching its AgentCard."""
        try:
            agent_card = await self._get_agent_card()
            return agent_card is not None
        except Exception:
            return False

    async def list_tools(self) -> list[dict[str, Any]]:
        """List available tools from the remote agent (from AgentCard.skills)."""
        try:
            agent_card = await self._get_agent_card()

            skills = list(agent_card.skills) if agent_card.skills else []

            all_tools = []
            for skill in skills:
                # A2A skills accept a natural-language message parameter
                parameters = {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": f"Natural language message to send to the '{skill.id}' tool.",
                        },
                    },
                    "required": ["message"],
                }
                tool_dict = {
                    "name": skill.id,
                    "description": skill.description,
                    "parameters": parameters,
                    "type": "skill",
                    "skill_info": {
                        "id": skill.id,
                        "name": skill.name,
                        "description": skill.description,
                        "tags": list(skill.tags),
                        "examples": list(skill.examples),
                    },
                }
                all_tools.append(tool_dict)

            return all_tools

        except Exception as e:
            raise ToolError(f"Error during tool listing: {e}") from e

    async def discover_capabilities(self) -> dict[str, Any]:
        """Discover the agent's capabilities."""
        try:
            agent_card = await self._get_agent_card()

            capabilities = {
                "agent_id": self.config.agent_id,
                "base_url": self.config.base_url,
                "name": agent_card.name,
                "description": agent_card.description,
                "version": agent_card.version,
                "streaming": agent_card.capabilities.streaming if agent_card.HasField("capabilities") else False,
                "skills": [{"id": s.id, "name": s.name, "description": s.description} for s in agent_card.skills],
            }

            return capabilities

        except Exception as e:
            raise ToolError(f"Error during capability discovery: {e}") from e

    async def _get_agent_card(self) -> AgentCard:
        """Fetch and cache the AgentCard via A2ACardResolver."""
        if self._agent_card is not None:
            return self._agent_card

        headers = {}
        if self.config.auth_token:
            headers["Authorization"] = f"Bearer {self.config.auth_token}"

        async with httpx.AsyncClient(headers=headers) as http_client:
            resolver = A2ACardResolver(httpx_client=http_client, base_url=self.config.base_url)
            self._agent_card = await resolver.get_agent_card()

        return self._agent_card


class A2AToolWrapper(BaseTool):
    """A2A tool wrapper (northbound API unchanged)."""

    def __init__(
        self,
        a2a_client: A2AClientWrapper,
        tool_name: str,
        tool_definition: dict[str, Any],
        category: str = "a2a",
        **kwargs,
    ):
        description = tool_definition.get("description", f"A2A tool: {tool_name}")
        super().__init__(tool_name, category, description, **kwargs)

        self.a2a_client = a2a_client
        self.tool_definition = tool_definition
        self.tool_type = ToolType.A2A_TOOL
        self.remote_tool_name = tool_definition.get("name", tool_name)
        self.parameters_schema = tool_definition.get("parameters", {})

    def call(self, **kwargs) -> ToolResult:
        """Execute A2A tool (sync)."""
        try:
            is_valid, error = self.validate_input(**kwargs)
            if not is_valid:
                return ToolResult(success=False, error=f"Invalid input parameters for A2A tool '{self.name}': {error}")

            result_data = None

            try:
                asyncio.get_running_loop()
                # In event loop: run in new thread with new event loop
                import concurrent.futures

                def run_in_new_loop():
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    try:
                        temp_client = A2AClientWrapper(self.a2a_client.config)
                        try:
                            result = new_loop.run_until_complete(temp_client.call_tool(self.remote_tool_name, kwargs))
                            return result
                        finally:
                            try:
                                new_loop.run_until_complete(temp_client.close())
                            except Exception as e:
                                logger = logging.getLogger(__name__)
                                logger.warning(f"Failed to close temp A2A client: {e}")
                    finally:
                        new_loop.close()

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(run_in_new_loop)
                    result_data = future.result(timeout=60)

            except RuntimeError:
                # No running event loop
                async def run_with_temp_client():
                    temp_client = A2AClientWrapper(self.a2a_client.config)
                    try:
                        return await temp_client.call_tool(self.remote_tool_name, kwargs)
                    finally:
                        await temp_client.close()

                result_data = asyncio.run(run_with_temp_client())

            return ToolResult(
                success=True,
                data=result_data,
                metadata={
                    "tool_type": "a2a_tool",
                    "agent_id": self.a2a_client.config.agent_id,
                    "base_url": self.a2a_client.config.base_url,
                    "remote_tool_name": self.remote_tool_name,
                },
            )

        except Exception as e:
            error_type = _classify_exception(e)
            return ToolResult(
                success=False,
                error=str(e),
                metadata={
                    "tool_type": "a2a_tool",
                    "error_type": type(e).__name__,
                    "agent_id": self.a2a_client.config.agent_id,
                },
                error_type=error_type,
            )

    async def acall(self, **kwargs) -> ToolResult:
        """Execute A2A tool (async)."""
        try:
            is_valid, error = self.validate_input(**kwargs)
            if not is_valid:
                return ToolResult(success=False, error=f"Invalid input parameters for A2A tool '{self.name}': {error}")

            # Always create fresh client to avoid connection state issues
            temp_client = A2AClientWrapper(self.a2a_client.config)
            try:
                result_data = await temp_client.call_tool(self.remote_tool_name, kwargs)

                return ToolResult(
                    success=True,
                    data=result_data,
                    metadata={
                        "tool_type": "a2a_tool",
                        "agent_id": self.a2a_client.config.agent_id,
                        "base_url": self.a2a_client.config.base_url,
                        "remote_tool_name": self.remote_tool_name,
                    },
                )

            finally:
                try:
                    await temp_client.close()
                except Exception as e:
                    logger = logging.getLogger(__name__)
                    logger.warning(f"Failed to close temp A2A client in async execution: {e}")

        except Exception as e:
            error_type = _classify_exception(e)
            return ToolResult(
                success=False,
                error=str(e),
                metadata={
                    "tool_type": "a2a_tool",
                    "error_type": type(e).__name__,
                    "agent_id": self.a2a_client.config.agent_id,
                },
                error_type=error_type,
            )

    def get_schema(self) -> ToolSchema:
        """Generate tool schema."""
        parameters = []

        if "properties" in self.parameters_schema:
            required_fields = self.parameters_schema.get("required", [])

            for prop_name, prop_def in self.parameters_schema["properties"].items():
                param_type = self._json_type_to_python_type(prop_def.get("type", "string"))
                is_required = prop_name in required_fields
                default_value = prop_def.get("default")
                description = prop_def.get("description", f"Parameter {prop_name}")

                parameters.append(
                    ParameterSchema(
                        name=prop_name,
                        type=param_type,
                        required=is_required,
                        default=default_value,
                        description=description,
                    )
                )

        return ToolSchema(self.name, self.description, parameters, "a2a_tool")

    def _json_type_to_python_type(self, json_type: str) -> type:
        """Map JSON Schema types to Python types."""
        type_mapping = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict}
        return type_mapping.get(json_type, str)


class A2AToolRegistry:
    """A2A agent connection registry — connections are shared across all Agents.

    Tool discovery (wrapping discovered tools into A2AToolWrapper) is done by
    per-Agent ToolManager, not stored here. This registry only manages agent
    connections (register, ping, cleanup).
    """

    def __init__(self):
        self._clients: dict[str, A2AClientWrapper] = {}
        self._agent_configs: dict[str, AgentConfig] = {}

    def register_agent(
        self,
        agent_id: str,
        base_url: str,
        auth_token: str | None = None,
        timeout: int = 30,
        category: str = "a2a",
        description: str = "",
    ) -> A2AClientWrapper:
        """Register an agent endpoint."""
        config = AgentConfig(
            agent_id=agent_id,
            base_url=base_url,
            auth_token=auth_token,
            timeout=timeout,
            category=category,
            description=description,
        )

        client = A2AClientWrapper(config)
        self._clients[agent_id] = client
        self._agent_configs[agent_id] = config
        return client

    async def list_agent_tools(self, agent_id: str) -> list[A2AToolWrapper]:
        """List raw A2A tools from an agent — caller (ToolManager) wraps them into per-Agent instances."""
        if agent_id not in self._clients:
            raise ToolError(f"Agent '{agent_id}' not registered")

        client = self._clients[agent_id]

        if not await client.ping():
            raise ToolError(f"Agent '{agent_id}' is not reachable")

        tools_list = await client.list_tools()
        return [A2AToolWrapper(client, tool_def["name"], tool_def) for tool_def in tools_list]

    def get_client(self, agent_id: str) -> A2AClientWrapper | None:
        """Get an agent's client wrapper by ID."""
        return self._clients.get(agent_id)

    async def health_check(self, agent_id: str | None = None) -> dict[str, bool]:
        """Check agent health."""
        if agent_id:
            if agent_id not in self._clients:
                return {agent_id: False}
            client = self._clients[agent_id]
            return {agent_id: await client.ping()}

        results = {}
        for aid, client in self._clients.items():
            results[aid] = await client.ping()
        return results

    def list_agents(self) -> list[str]:
        """List all registered agent IDs."""
        return list(self._clients.keys())

    async def cleanup(self):
        """Clean up all A2A connections."""
        logger = logging.getLogger(__name__)
        logger.debug("Starting A2A registry cleanup for %d clients", len(self._clients))

        total = len(self._clients)
        self._clients.clear()
        self._agent_configs.clear()

        logger.debug("A2A registry cleanup completed successfully")
        return {"total": total, "successful": total, "failed": 0}


# Global A2A agent connection registry (shared across all Agents)
a2a_registry = A2AToolRegistry()
