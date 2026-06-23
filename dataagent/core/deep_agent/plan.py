# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Incremental OpenJiuWen DeepAgent build plan."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openjiuwen.core.foundation.tool import McpServerConfig, Tool, ToolCard

    from dataagent.core.deep_agent.builders.tools.a2a import A2AAgentBinding


@dataclass
class DeepAgentBuildPlan:
    """Collect build contributions while rejecting ambiguous ability names."""

    tools: list[Tool | ToolCard | Any] = field(default_factory=list)
    mcps: list[McpServerConfig | Any] = field(default_factory=list)
    a2a_agents: list[A2AAgentBinding | Any] = field(default_factory=list)
    _tool_sources: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _mcp_sources: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _agent_sources: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _agent_name_sources: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def add_tools(self, tools: list[Any], *, source: str) -> None:
        for tool in tools:
            name = _tool_name(tool)
            if name:
                previous_source = self._tool_sources.get(name)
                if previous_source is not None:
                    raise ValueError(
                        f"Tool name {name!r} from {source} conflicts with a tool from {previous_source}"
                    )
                self._tool_sources[name] = source
            self.tools.append(tool)

    def add_mcps(self, mcps: list[Any], *, source: str) -> None:
        for mcp in mcps:
            server_id = getattr(mcp, "server_id", None)
            if server_id:
                previous_source = self._mcp_sources.get(server_id)
                if previous_source is not None:
                    raise ValueError(
                        f"MCP server_id {server_id!r} from {source} conflicts with a server from {previous_source}"
                    )
                self._mcp_sources[server_id] = source
            self.mcps.append(mcp)

    def add_a2a_agents(self, bindings: list[Any], *, source: str) -> None:
        for binding in bindings:
            agent_id = getattr(getattr(binding, "card", None), "id", None)
            name = getattr(getattr(binding, "card", None), "name", None)
            if agent_id:
                previous_source = self._agent_sources.get(agent_id)
                if previous_source is not None:
                    raise ValueError(
                        f"A2A agent id {agent_id!r} from {source} conflicts with an agent from {previous_source}"
                    )
                self._agent_sources[agent_id] = source
            if name:
                previous_source = self._agent_name_sources.get(name)
                if previous_source is not None:
                    raise ValueError(
                        f"A2A ability name {name!r} from {source} conflicts with an agent from {previous_source}"
                    )
                self._agent_name_sources[name] = source
            self.a2a_agents.append(binding)


def _tool_name(tool: Any) -> str | None:
    card = getattr(tool, "card", None)
    name = getattr(card, "name", None) if card is not None else getattr(tool, "name", None)
    return str(name) if name else None
