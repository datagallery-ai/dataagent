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
import asyncio
import threading
from dataclasses import dataclass
from typing import Any, Literal, cast
from weakref import WeakKeyDictionary

import httpx
from loguru import logger

# 官方MCP库导入
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, ImageContent, TextContent
from mcp.types import Tool as MCPTool

from dataagent.core.managers.action_manager.base import (
    BaseTool,
    ErrorType,
    ToolError,
    ToolResult,
    ToolType,
    classify_exception,
)
from dataagent.core.managers.action_manager.schemas import ParameterSchema, ToolSchema


def _classify_exception(exc: Exception) -> ErrorType:
    """根据异常类型分类错误（保持向后兼容，内部委托给统一函数）"""
    err_type, _ = classify_exception(exc)
    return err_type


@dataclass
class MCPServerConfig:
    """MCP服务器配置 - 支持stdio和sse两种transport类型"""

    server_id: str
    transport_type: Literal["stdio", "sse", "streamable_http"] = "stdio"  # 传输协议类型
    config: dict[str, Any] | None = None  # 传输协议配置
    category: str = "mcp"  # 服务器分类
    description: str = ""  # 服务器描述

    def __post_init__(self):
        if self.config is None:
            self.config = {}

        # 验证必需参数
        self._validate_config()

    @classmethod
    def create_stdio_config(
        cls,
        server_id: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ):
        """创建STDIO配置的便利方法"""
        config = {"command": command, "args": args or [], "env": env or {}, "cwd": cwd}
        return cls(server_id=server_id, transport_type="stdio", config=config)

    @classmethod
    def create_sse_config(cls, server_id: str, host: str, port: int, **kwargs):
        """创建SSE配置的便利方法"""
        config = {"host": host, "port": port, **kwargs}
        return cls(server_id=server_id, transport_type="sse", config=config)

    # 便利方法用于获取配置值
    def get_config(self, key: str, default=None):
        """获取配置值"""
        config = self.config or {}
        return config.get(key, default)

    def _validate_config(self):
        """根据transport类型验证配置"""
        config = self.config or {}
        if self.transport_type == "stdio":
            required = ["command"]
            missing = [k for k in required if k not in config]
            if missing:
                raise ValueError(f"STDIO transport missing required config: {missing}")

        elif self.transport_type == "sse":
            # SSE可以使用url或者host+port
            has_url = "url" in config
            has_host_port = "host" in config and "port" in config
            if not (has_url or has_host_port):
                raise ValueError("SSE transport requires either 'url' or both 'host' and 'port'")

        elif self.transport_type == "streamable_http":
            if not str(config.get("url") or "").strip():
                raise ValueError("streamable_http transport requires 'url'")

        else:
            raise ValueError(
                f"Unsupported transport type: {self.transport_type}. "
                "Only 'stdio', 'sse', and 'streamable_http' are supported."
            )


class MCPClientWrapper:
    """基于官方MCP库的客户端包装器 - 支持连接池管理"""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._session: ClientSession | None = None
        self._connected = False
        self._client_context = None
        self._read_stream = None
        self._write_stream = None
        # Per running event loop: asyncio.Lock cannot be shared across loops/threads.
        self._connection_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakKeyDictionary()
        self._connection_locks_guard = threading.Lock()
        self._last_used = None
        self._connection_timeout = 300  # 5分钟连接超时

        # 根据transport类型初始化不同的参数
        self.transport_type = self.config.transport_type
        self._transport_params = self._create_transport_params()

    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.disconnect()
        return False

    @staticmethod
    async def create_temporary_connection(config: MCPServerConfig):
        """创建临时连接的工厂方法，用于上下文管理器"""
        return MCPClientWrapper(config)

    async def connect(self):
        """连接到MCP服务器并保持持久化连接"""
        if self._connected and self._session:
            return

        try:
            # 根据transport类型创建客户端
            if self.transport_type == "stdio":
                self._client_context = stdio_client(cast(StdioServerParameters, self._transport_params))
            elif self.transport_type == "sse":
                params = cast(dict[str, Any], self._transport_params)
                self._client_context = sse_client(
                    params["url"],
                    headers=params.get("headers", {}),
                    timeout=params.get("timeout", 30),
                )
            else:
                raise ValueError(
                    f"Unsupported transport type: {self.transport_type}. \
                                 Only 'stdio' and 'sse' are supported."
                )

            # 进入上下文管理器获取读写流
            self._read_stream, self._write_stream = await self._client_context.__aenter__()

            # 创建持久化的ClientSession
            self._session = ClientSession(self._read_stream, self._write_stream)
            await self._session.initialize()

        except Exception as e:
            # 清理部分初始化的资源
            await self._cleanup_on_error()
            raise ToolError(f"Failed to connect to MCP server '{self.config.server_id}': {e}") from e
        self._connected = True

    async def disconnect(self):
        """断开与MCP服务器的连接"""
        if not self._connected:
            return

        # 注意：ClientSession没有close方法，它由stdio_client管理
        if self._client_context:
            try:
                await self._client_context.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Warning: Error during disconnect: {e}")

        self._connected = False
        self._session = None
        self._client_context = None
        self._read_stream = None
        self._write_stream = None

    async def list_tools(self) -> list[MCPTool]:
        """列出MCP服务器上的可用工具"""

        async def _operation(session):
            response = await session.list_tools()
            return response.tools

        try:
            return await self._execute_with_connection(_operation)
        except Exception as e:
            raise ToolError(f"Failed to list tools from MCP server '{self.config.server_id}': {e}") from e

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        """调用MCP工具"""

        async def _operation(session):
            return await session.call_tool(name=name, arguments=arguments)

        try:
            return await self._execute_with_connection(_operation)
        except Exception as e:
            raise ToolError(f"Failed to call tool '{name}' on MCP server '{self.config.server_id}': {e}") from e

    async def ping(self) -> bool:
        """检查连接状态"""
        try:
            # 通过列出工具来检查连接
            await self.list_tools()
            return True
        except Exception:
            return False

    async def _ensure_connected(self):
        """确保连接已建立，如果没有则自动连接"""
        if not self._connected or not self._session:
            await self.connect()

    async def _with_retry(self, operation, max_retries: int = 2):
        """带重试机制的操作执行"""
        for attempt in range(max_retries + 1):
            try:
                await self._ensure_connected()
                return await operation()
            except Exception as e:
                if attempt < max_retries:
                    # 重连并重试
                    await self.disconnect()
                    continue
                raise e

    def _get_connection_lock(self) -> asyncio.Lock:
        """Return an ``asyncio.Lock`` bound to the currently running event loop.

        Resource jobs and sync MCP tool calls often invoke this client via
        ``asyncio.run`` from worker threads. A single process-wide
        ``asyncio.Lock`` becomes bound to the first loop that acquires it and
        then raises ``RuntimeError`` when reused from another loop. Keep one
        lock per running loop instead.
        """
        loop = asyncio.get_running_loop()
        with self._connection_locks_guard:
            lock = self._connection_locks.get(loop)
            if lock is None:
                lock = asyncio.Lock()
                self._connection_locks[loop] = lock
            return lock

    async def _execute_with_connection(self, operation):
        """使用连接执行操作的通用方法"""
        async with self._get_connection_lock():
            if self.transport_type == "stdio":
                async with stdio_client(cast(StdioServerParameters, self._transport_params)) as (  # noqa: SIM117
                    read_stream,
                    write_stream,
                ):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        return await operation(session)

            if self.transport_type == "sse":
                params = cast(dict[str, Any], self._transport_params)
                async with sse_client(  # noqa: SIM117
                    params["url"],
                    headers=params.get("headers", {}),
                    timeout=params.get("timeout", 30),
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        return await operation(session)

            if self.transport_type == "streamable_http":
                params = cast(dict[str, Any], self._transport_params)
                headers = params.get("headers") or {}
                timeout = float(params.get("timeout", 30))
                async with (
                    httpx.AsyncClient(
                        headers=headers,
                        timeout=httpx.Timeout(timeout),
                    ) as http_client,
                    streamable_http_client(params["url"], http_client=http_client) as (
                        read_stream,
                        write_stream,
                        _get_session_id,
                    ),
                ):
                    del _get_session_id
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        return await operation(session)

            raise ValueError(f"Unsupported transport type: {self.transport_type}")

    async def _cleanup_on_error(self):
        """错误时清理资源"""
        if self._client_context:
            try:
                await self._client_context.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error during cleanup after failed connection: {e}")
        self._connected = False
        self._session = None
        self._client_context = None
        self._read_stream = None
        self._write_stream = None

    def _create_transport_params(self):
        """根据transport类型创建相应的参数"""
        transport_config = self.config.config or {}
        if self.transport_type == "stdio":
            return StdioServerParameters(
                command=transport_config["command"],
                args=transport_config.get("args") or [],
                env=transport_config.get("env"),
                cwd=transport_config.get("cwd"),
            )
        if self.transport_type == "sse":
            # SSE transport参数
            return transport_config
        if self.transport_type == "streamable_http":
            return transport_config
        raise ValueError(
            f"Unsupported transport type: {self.transport_type}. "
            "Only 'stdio', 'sse', and 'streamable_http' are supported."
        )


class MCPToolWrapper(BaseTool):
    """基于官方MCP库的工具包装器"""

    def __init__(self, mcp_client: MCPClientWrapper, mcp_tool: MCPTool, category: str = "mcp", **kwargs):
        # 从官方MCPTool对象提取信息
        tool_name = f"{mcp_tool.name}"
        description = mcp_tool.description or f"MCP tool: {mcp_tool.name}"

        super().__init__(tool_name, category, description, **kwargs)

        self.mcp_client = mcp_client
        self.mcp_tool = mcp_tool
        self.tool_type = ToolType.MCP_TOOL
        self.input_schema = mcp_tool.inputSchema

    def call(self, **kwargs) -> ToolResult:
        """执行MCP工具"""
        try:
            # 验证输入参数
            is_valid, error = self.get_schema().validate_input(kwargs)
            if not is_valid:
                return ToolResult(success=False, error=f"Invalid input parameters for MCP tool '{self.name}': {error}")
            # 异步调用MCP工具
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 如果已经在事件循环中，创建新的任务
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, self._async_call(**kwargs))
                    result_data = future.result()
            else:
                # 如果没有事件循环，直接运行
                result_data = asyncio.run(self._async_call(**kwargs))

            return ToolResult(
                success=True,
                data=result_data,
                metadata={
                    "tool_type": "mcp_tool",
                    "server_id": self.mcp_client.config.server_id,
                    "tool_name": self.mcp_tool.name,
                    "mcp_tool_description": self.mcp_tool.description,
                },
            )

        except Exception as e:
            error_type = _classify_exception(e)
            return ToolResult(
                success=False,
                error=str(e),
                metadata={
                    "tool_type": "mcp_tool",
                    "error_type": type(e).__name__,
                    "server_id": self.mcp_client.config.server_id,
                    "tool_name": self.mcp_tool.name,
                },
                error_type=error_type,
            )

    def get_schema(self) -> ToolSchema:
        """生成工具Schema"""
        parameters = []

        if "properties" in self.input_schema:
            required_fields = self.input_schema.get("required", [])

            for prop_name, prop_def in self.input_schema["properties"].items():
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

        return ToolSchema(self.name, self.description, parameters, "mcp_tool")

    async def _async_call(self, **kwargs) -> Any:
        """异步调用MCP工具 - 使用持久化连接"""
        # 直接调用MCP工具，连接管理由MCPClientWrapper处理
        response = await self.mcp_client.call_tool(self.mcp_tool.name, kwargs)

        # 处理官方MCP库的响应格式
        if hasattr(response, "content") and response.content:
            content_parts = []
            for item in response.content:
                if isinstance(item, TextContent):
                    content_parts.append(item.text)
                elif isinstance(item, ImageContent):
                    content_parts.append(f"[Image: {item.data}]")
                else:
                    # 处理其他内容类型
                    content_parts.append(f"[Content: {str(item)}]")

            return "\n".join(content_parts)

        # 如果没有content字段，返回原始响应
        return response.model_dump() if hasattr(response, "model_dump") else str(response)

    def _json_type_to_python_type(self, json_type: str) -> type:
        """将JSON Schema类型转换为Python类型"""
        type_mapping = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict}
        return type_mapping.get(json_type, str)


class MCPToolRegistry:
    """MCP server connection registry — connections are shared across all Agents.

    Tool discovery (wrapping discovered tools into MCPToolWrapper) is done by
    per-Agent ToolManager, not stored here. This registry only manages server
    connections (register, ping, cleanup).
    """

    def __init__(self):
        self._clients: dict[str, MCPClientWrapper] = {}

    def register_server(
        self,
        server_id: str,
        transport_type: str,
        config: dict[str, Any],
        category: str = "general",
        description: str = "",
    ):
        """注册MCP服务器

        Args:
            server_id: 服务器ID
            transport_type: transport类型 ('stdio' 或 'sse')
            config: transport配置字典
            category: 服务器分类
            description: 服务器描述
        """
        if transport_type not in {"stdio", "sse", "streamable_http"}:
            raise ToolError(f"Unsupported transport type: {transport_type}")

        server_config = MCPServerConfig(
            server_id=server_id,
            transport_type=cast(Literal["stdio", "sse", "streamable_http"], transport_type),
            config=config,
            category=category,
            description=description,
        )
        client = MCPClientWrapper(server_config)
        self._clients[server_id] = client
        return client

    async def list_server_tools(self, server_id: str) -> list[MCPToolWrapper]:
        """List raw MCP tools from a server — caller (ToolManager) wraps them into per-Agent instances."""
        if server_id not in self._clients:
            raise ToolError(f"MCP server '{server_id}' not registered")

        client = self._clients[server_id]
        mcp_tools = await client.list_tools()
        return [MCPToolWrapper(client, mcp_tool) for mcp_tool in mcp_tools]

    def get_client(self, server_id: str) -> MCPClientWrapper | None:
        """Get a server's client wrapper by ID."""
        return self._clients.get(server_id)

    def list_servers(self) -> list[str]:
        """列出所有服务器ID"""
        return list(set(self._clients.keys()))

    async def ping_server(self, server_id: str) -> bool:
        """检查服务器连接状态"""
        if server_id in self._clients:
            return await self._clients[server_id].ping()
        return False

    async def cleanup(self):
        """清理所有MCP连接"""
        for client in self._clients.values():
            await client.disconnect()


# 全局 MCP server connection registry (shared across all Agents)
mcp_registry = MCPToolRegistry()
