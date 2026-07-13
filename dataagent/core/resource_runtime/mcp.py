# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""MCP client wiring for executable resource jobs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from mcp.types import CallToolResult, TextContent

from dataagent.actions.tools.mcp import MCPClientWrapper, MCPServerConfig
from dataagent.core.resource_runtime.operations.protocols import McpResourceClient
from dataagent.resources.catalog.models import Resource
from dataagent.resources.drivers.mcp_resource import resolve_mcp_transport
from dataagent.resources.resolve.prepare import DriverBinding


def mcp_server_config_from_binding(resource_id: str, driver: DriverBinding) -> MCPServerConfig:
    """Build MCP server config from a resolved :class:`DriverBinding`.

    Args:
        resource_id: Stable resource id used as MCP ``server_id``.
        driver: Resolved driver binding with plain MCP connection fields.

    Returns:
        MCP client configuration for :class:`MCPClientWrapper`.
    """
    return MCPServerConfig(
        server_id=f"resource:{resource_id}",
        transport_type="streamable_http",
        config={
            "url": driver.mcp_url,
            "headers": dict(driver.mcp_headers or {}),
            "timeout": int(driver.mcp_timeout_sec),
        },
        category="resource",
        description=f"Resource MCP backend for {resource_id}",
    )


def build_mcp_resource_client(resource: Resource) -> McpResourceClient:
    """Build one MCP resource client from a resource definition.

    Args:
        resource: Executable MCP-backed resource definition.

    Returns:
        Client implementing :class:`McpResourceClient`.
    """
    resolved = resolve_mcp_transport(resource.id, resource.transport)
    server_config = MCPServerConfig(
        server_id=f"resource:{resource.id}",
        transport_type="streamable_http",
        config={
            "url": resolved["url"],
            "headers": resolved["headers"],
            "timeout": resolved["timeout_sec"],
        },
        category="resource",
        description=f"Resource MCP backend for {resource.id}",
    )
    return McpResourceClientAdapter(MCPClientWrapper(server_config))


def build_mcp_client_from_driver(resource_id: str, driver: DriverBinding) -> McpResourceClient:
    """Build one MCP client from a resolved driver binding.

    Args:
        resource_id: Resource id for MCP server naming.
        driver: Resolved MCP driver binding.

    Returns:
        Client implementing :class:`McpResourceClient`.
    """
    return McpResourceClientAdapter(MCPClientWrapper(mcp_server_config_from_binding(resource_id, driver)))


def default_mcp_client_factory() -> Callable[[Resource], McpResourceClient]:
    """Return the default MCP client factory for executable MCP resources."""
    return build_mcp_resource_client


class McpResourceClientAdapter:
    """Adapter that exposes :class:`MCPClientWrapper` through :class:`McpResourceClient`."""

    def __init__(self, client: MCPClientWrapper) -> None:
        """Wrap one MCP client for resource operation calls."""
        self._client = client

    def call_tool_sync(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke one remote MCP tool and return a normalized operation result."""
        return call_resource_mcp_tool_sync(self._client, tool_name, arguments)


def call_resource_mcp_tool_sync(
    client: MCPClientWrapper,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Invoke one MCP tool synchronously from a resource job runner thread."""
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(asyncio.run, _async_call_resource_mcp_tool(client, tool_name, arguments))
                raw = future.result()
        else:
            raw = asyncio.run(_async_call_resource_mcp_tool(client, tool_name, arguments))
        normalized = normalize_mcp_call_tool_result(raw)
        return normalized if isinstance(normalized, dict) else {"result": normalized}
    except Exception as exc:
        return {
            "status": "error",
            "error": format_mcp_call_exception(client, tool_name, exc),
            "exit_code": 1,
        }


async def _async_call_resource_mcp_tool(
    client: MCPClientWrapper,
    tool_name: str,
    arguments: dict[str, Any],
) -> CallToolResult:
    """Async helper used by :func:`call_resource_mcp_tool_sync`."""
    return await client.call_tool(str(tool_name or "").strip(), dict(arguments or {}))


def normalize_mcp_call_tool_result(payload: Any) -> dict[str, Any]:
    """Normalize MCP ``call_tool`` results into resource-operation dicts."""
    if isinstance(payload, CallToolResult):
        structured = getattr(payload, "structuredContent", None)
        if isinstance(structured, dict):
            return dict(structured)
        content = getattr(payload, "content", None)
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, TextContent):
                    continue
                text = str(item.text or "").strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    status = "error" if getattr(payload, "isError", False) else "completed"
                    return {"status": status, "summary": text}
                if isinstance(parsed, dict):
                    return parsed
                return {"status": "completed", "result": parsed}
        if getattr(payload, "isError", False):
            return {"status": "error", "error": _summary_from_call_result(payload)}
        dumped = payload.model_dump() if hasattr(payload, "model_dump") else {"result": str(payload)}
        return dumped if isinstance(dumped, dict) else {"result": dumped}

    if isinstance(payload, dict):
        structured = payload.get("structuredContent")
        if isinstance(structured, dict):
            return dict(structured)
        content = payload.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    status = "error" if payload.get("isError") else "completed"
                    return {"status": status, "summary": text}
                return parsed if isinstance(parsed, dict) else {"status": "completed", "result": parsed}
        if payload.get("isError"):
            return {"status": "error", "error": _summary_from_mapping(payload)}
        return dict(payload)
    return {"status": "completed", "result": payload}


def _summary_from_call_result(payload: CallToolResult) -> str:
    """Extract a short error summary from a call-tool result."""
    content = getattr(payload, "content", None)
    if isinstance(content, list):
        parts = [str(item.text) for item in content if isinstance(item, TextContent) and str(item.text or "").strip()]
        if parts:
            return "\n".join(parts)
    return "MCP tool call failed"


def _summary_from_mapping(payload: dict[str, Any]) -> str:
    """Extract a short error summary from a normalized mapping."""
    for key in ("error", "summary", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(payload)


def format_mcp_call_exception(client: MCPClientWrapper, tool_name: str, exc: Exception) -> str:
    """Format one MCP transport/tool exception into a resource-friendly message."""
    message = str(exc).strip() or exc.__class__.__name__
    lowered = message.lower()
    if "connect" in lowered or "connection refused" in lowered or "unhandled errors in a taskgroup" in lowered:
        url = str((client.config.config or {}).get("url") or "").strip()
        server_id = str(client.config.server_id or "").strip()
        target = url or server_id or "configured MCP endpoint"
        return f"MCP server unreachable at {target} while calling {tool_name}: {message}"
    return f"Failed to call MCP tool {tool_name}: {message}"
