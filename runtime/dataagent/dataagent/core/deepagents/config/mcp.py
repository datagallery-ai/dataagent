"""Compile legacy MCP declarations with the official LangChain MCP adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, cast

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import Connection

from dataagent.core.deepagents.config.tool_hooks import tag_tool


class MCPToolConfigCompiler:
    """Compile ``TOOLS.mcp_servers`` into native LangChain MCP tools."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = config

    async def compile(self) -> tuple[BaseTool, ...]:
        """Discover configured MCP tools through ``langchain-mcp-adapters``."""
        connections = self._compile_connections()
        if not connections:
            return ()
        client = MultiServerMCPClient(connections)
        discovered = await asyncio.gather(*(client.get_tools(server_name=server_id) for server_id in connections))
        return tuple(
            tag_tool(tool, "mcp", server_id)
            for server_id, server_tools in zip(connections, discovered, strict=True)
            for tool in server_tools
        )

    def _compile_connections(self) -> dict[str, Connection]:
        tools_config = self._tools_config()
        raw_entries = tools_config.get("mcp_servers", [])
        if raw_entries is None:
            return {}
        if isinstance(raw_entries, (str, bytes)) or not isinstance(raw_entries, Sequence):
            raise ValueError("TOOLS.mcp_servers must be a list.")

        connections: dict[str, Connection] = {}
        for index, raw_entry in enumerate(raw_entries):
            path = f"TOOLS.mcp_servers[{index}]"
            entry = self._as_mapping(raw_entry, path)
            server_id = str(entry.get("server_id") or entry.get("name") or "").strip()
            if not server_id:
                raise ValueError(f"{path}.server_id is required.")
            if connections.get(server_id) is not None:
                raise ValueError(f"Duplicate MCP server id: {server_id!r}.")
            connections.update({server_id: self._compile_connection(entry, path)})
        return connections

    def _compile_connection(self, entry: Mapping[str, Any], path: str) -> Connection:
        config = self._as_mapping(entry.get("config", {}), f"{path}.config")
        transport = self._resolve_transport(entry, config, path)
        connection: dict[str, Any] = {"transport": transport}

        if transport == "stdio":
            command = str(config.get("command") or entry.get("command") or "").strip()
            if not command:
                raise ValueError(f"{path}.config.command is required for stdio transport.")
            args = self._as_string_list(config.get("args", entry.get("args", [])), f"{path}.config.args")
            connection.update({"command": command, "args": args})
            self._copy_optional_stdio_fields(connection, config, entry)
            return cast("Connection", connection)

        url = self._resolve_url(config, entry, path)
        connection.update({"url": url})
        self._copy_optional_http_fields(connection, config, entry)
        return cast("Connection", connection)

    def _tools_config(self) -> Mapping[str, Any]:
        raw = self._config.get("TOOLS", {})
        if raw is None:
            return {}
        return self._as_mapping(raw, "TOOLS")

    @staticmethod
    def _as_mapping(value: Any, path: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{path} must be a mapping.")
        return value

    @staticmethod
    def _as_string_list(value: Any, path: str) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise ValueError(f"{path} must be a list of strings.")
        return [str(item) for item in value]

    @staticmethod
    def _resolve_transport(
        entry: Mapping[str, Any],
        config: Mapping[str, Any],
        path: str,
    ) -> str:
        raw = entry.get("transport_type") or entry.get("transport") or config.get("transport") or ""
        transport = str(raw).strip().lower().replace("-", "_")
        aliases = {"http": "streamable_http", "ws": "websocket"}
        transport = aliases.get(transport, transport)
        if transport not in {"stdio", "sse", "streamable_http", "websocket"}:
            raise ValueError(f"{path}.transport_type must be one of stdio, sse, streamable_http, or websocket.")
        return transport

    @staticmethod
    def _resolve_url(config: Mapping[str, Any], entry: Mapping[str, Any], path: str) -> str:
        url = str(config.get("url") or entry.get("url") or "").strip()
        if url:
            return url

        host = str(config.get("host") or entry.get("host") or "").strip()
        port = config.get("port", entry.get("port"))
        if not host or port is None:
            raise ValueError(f"{path}.config.url is required for this MCP transport.")
        scheme = str(config.get("scheme") or entry.get("scheme") or "http").strip()
        base_url = host if "://" in host else f"{scheme}://{host}"
        path_value = str(config.get("path") or entry.get("path") or "").strip()
        suffix = f"/{path_value.lstrip('/')}" if path_value else ""
        return f"{base_url}:{port}{suffix}"

    @staticmethod
    def _copy_optional_stdio_fields(
        connection: dict[str, Any],
        config: Mapping[str, Any],
        entry: Mapping[str, Any],
    ) -> None:
        for key in ("env", "cwd", "encoding", "encoding_error_handler", "session_kwargs"):
            value = config.get(key, entry.get(key))
            if value is not None:
                connection.update({key: value})

    @staticmethod
    def _copy_optional_http_fields(
        connection: dict[str, Any],
        config: Mapping[str, Any],
        entry: Mapping[str, Any],
    ) -> None:
        for key in ("headers", "timeout", "sse_read_timeout", "terminate_on_close", "session_kwargs"):
            value = config.get(key, entry.get(key))
            if value is not None:
                connection.update({key: value})
