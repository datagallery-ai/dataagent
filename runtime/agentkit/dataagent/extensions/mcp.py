"""Discover native LangChain MCP tools; each call owns its upstream MCP session."""

import asyncio
import re
from datetime import timedelta

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from dataagent.bootstrap import Runtime
from dataagent.diagnostics import safe_error
from dataagent.extensions.loading import select_plugins


async def load_mcp_tools(runtime: Runtime) -> list[BaseTool]:
    """Load schemas once; returned native tools reconnect per invocation, without retries."""
    if not runtime.extensions.mcp_servers:
        return []
    # Validate selected manifests before starting any external process.
    select_plugins(runtime.settings.plugins, runtime.extensions.plugin_roots)
    connections = {}
    for name, config in runtime.extensions.mcp_servers.items():
        connection = dict(config)
        if connection["transport"] == "stdio":
            connection.update(args=list(connection["args"]), env=dict(connection["env"]),
                              session_kwargs={"read_timeout_seconds": timedelta(seconds=runtime.timeout_seconds)})
        else:
            connection.update(headers=dict(connection["headers"]), timeout=runtime.timeout_seconds,
                              sse_read_timeout=runtime.timeout_seconds)
        connections[name] = connection
    client = MultiServerMCPClient(connections, tool_name_prefix=True)
    tools = []
    # A single deadline fits inside the launcher's 30-second readiness budget.
    server = None
    try:
        async with asyncio.timeout(min(20, runtime.timeout_seconds)):
            for server in connections:
                try:
                    tools.extend(await client.get_tools(server_name=server))
                except Exception as error:
                    while isinstance(error, BaseExceptionGroup):
                        error = error.exceptions[0]
                    detail = safe_error(error, runtime.redaction_secrets)["message"]
                    raise ValueError(f"MCP discovery failed for {server}: {detail}") from None
    except TimeoutError:
        raise ValueError(f"MCP discovery timed out at {server}") from None
    seen = set()
    for tool in tools:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.name):
            raise ValueError("MCP returned a tool name outside [A-Za-z0-9_-]{1,64}")
        if tool.name in seen:
            raise ValueError(f"Duplicate MCP tool: {tool.name}")
        seen.add(tool.name)
    return tools
