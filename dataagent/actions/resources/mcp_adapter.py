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
"""MCP adapter for executable resource ``transport.type: mcp`` (Phase B2)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from typing import Any

from mcp.types import CallToolResult, TextContent

from dataagent.actions.tools.mcp import MCPClientWrapper, MCPServerConfig
from dataagent.core.resources.models import Resource
from dataagent.core.resources.protocols import McpResourceClient


def resolve_secret_ref(value: Any) -> str:
    """Resolve a header value that may use ``secret_ref: env:<VAR>``.

    Args:
        value: Plain string or mapping ``{"secret_ref": "env:VAR_NAME"}``.

    Returns:
        Resolved secret string, or ``str(value)`` for plain scalars.

    Raises:
        ValueError: When ``secret_ref`` scheme is unsupported or env var is missing.
    """
    if isinstance(value, Mapping):
        secret_ref = str(value.get("secret_ref") or "").strip()
        if not secret_ref:
            raise ValueError("secret_ref value is required when header value is an object")
        if secret_ref.startswith("env:"):
            var_name = secret_ref[len("env:") :].strip()
            if not var_name:
                raise ValueError("secret_ref env: requires a variable name")
            env_value = os.environ.get(var_name)
            if env_value is None:
                raise ValueError(f"environment variable is not set: {var_name}")
            return str(env_value)
        raise ValueError(f"unsupported secret_ref scheme: {secret_ref}")
    return str(value or "")


def resolve_transport_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """Resolve ``transport.headers`` values for MCP HTTP connections.

    Args:
        headers: Raw header mapping from resource transport configuration.

    Returns:
        Plain string header mapping suitable for httpx/MCP clients.
    """
    resolved: dict[str, str] = {}
    for key, value in (headers or {}).items():
        header_name = str(key or "").strip()
        if not header_name:
            continue
        resolved[header_name] = resolve_secret_ref(value)
    return resolved


def resource_transport_to_server_config(resource_id: str, transport: Mapping[str, Any]) -> MCPServerConfig:
    """Build an :class:`MCPServerConfig` from one resource ``transport`` block.

    Args:
        resource_id: Stable resource id used as MCP ``server_id``.
        transport: Executable resource transport mapping.

    Returns:
        MCP client configuration for :class:`MCPClientWrapper`.

    Raises:
        ValueError: When transport is missing required MCP fields.
    """
    url = str(transport.get("url") or "").strip()
    if not url:
        raise ValueError(f"resource {resource_id} MCP transport requires url")
    headers = resolve_transport_headers(
        transport.get("headers") if isinstance(transport.get("headers"), Mapping) else {}
    )
    timeout_sec = max(1, int(transport.get("timeout_sec") or 30))
    return MCPServerConfig(
        server_id=f"resource:{resource_id}",
        transport_type="streamable_http",
        config={"url": url, "headers": headers, "timeout": timeout_sec},
        category="resource",
        description=f"Resource MCP backend for {resource_id}",
    )


def normalize_mcp_call_tool_result(payload: Any) -> dict[str, Any]:
    """Normalize MCP ``call_tool`` results into resource-operation dicts.

    Args:
        payload: :class:`~mcp.types.CallToolResult` or plain mapping.

    Returns:
        JSON-serializable operation result with ``status`` / ``job_id`` when present.
    """
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


def call_resource_mcp_tool_sync(
    client: MCPClientWrapper,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Invoke one MCP tool synchronously from a resource job runner thread.

    Args:
        client: Connected or connect-on-demand MCP client wrapper.
        tool_name: Remote MCP tool name from ``operations.*``.
        arguments: Driver-assembled operation arguments.

    Returns:
        Normalized operation result dictionary.
    """
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


class McpResourceClientAdapter:
    """Adapter that exposes :class:`MCPClientWrapper` through :class:`McpResourceClient`."""

    def __init__(self, client: MCPClientWrapper) -> None:
        """Wrap one MCP client for resource operation calls."""
        self._client = client

    def call_tool_sync(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke one remote MCP tool and return a normalized operation result."""
        return call_resource_mcp_tool_sync(self._client, tool_name, arguments)


def build_mcp_resource_client(resource: Resource) -> McpResourceClient:
    """Build one MCP resource client from a resource transport block.

    Args:
        resource: Executable MCP-backed resource definition.

    Returns:
        Client implementing :class:`McpResourceClient`.
    """
    server_config = resource_transport_to_server_config(resource.id, resource.transport)
    return McpResourceClientAdapter(MCPClientWrapper(server_config))


async def _async_call_resource_mcp_tool(
    client: MCPClientWrapper,
    tool_name: str,
    arguments: dict[str, Any],
) -> CallToolResult:
    """Async helper used by :func:`call_resource_mcp_tool_sync`."""
    return await client.call_tool(str(tool_name or "").strip(), dict(arguments or {}))


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
    """Format one MCP transport/tool exception into a resource-friendly message.

    Args:
        client: MCP client wrapper used for the failed call.
        tool_name: Remote MCP tool name.
        exc: Raised exception from the MCP client stack.

    Returns:
        Human-readable error string for resource job consumers.
    """
    message = str(exc).strip() or exc.__class__.__name__
    lowered = message.lower()
    if "connect" in lowered or "connection refused" in lowered or "unhandled errors in a taskgroup" in lowered:
        url = str((client.config.config or {}).get("url") or "").strip()
        server_id = str(client.config.server_id or "").strip()
        target = url or server_id or "configured MCP endpoint"
        return f"MCP server unreachable at {target} while calling {tool_name}: {message}"
    return f"Failed to call MCP tool {tool_name}: {message}"
