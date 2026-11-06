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
"""Call DataOps MCP without registering its execution tools on the agent."""

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from dataagent.model_stream import collect_model_stream
from dataagent.models import ModelContext

# Bound by the public tool for this invocation, isolated across concurrent sessions.
settings = ContextVar("dataops_settings", default=None)


async def call_mcp(
    name: str, arguments: dict, *, integration: bool = False, config: dict | None = None
) -> dict:
    config = (settings.get() or {}) if config is None else config
    url = config.get("integration_url") if integration else config.get("url")
    url = url or os.environ.get("DATAOPS_MCP_URL", "")
    if not url:
        return {"success": False, "error": "DataOps MCP URL is not configured"}
    auth = config.get("authorization") or os.environ.get("DATAOPS_AUTHORIZATION", "")
    timeout = float(
        config.get("request_timeout_seconds", os.environ.get("DATAOPS_MCP_TIMEOUT", "300"))
    )
    headers = {"Authorization": auth} if auth else {}
    async with httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(timeout, read=timeout),
    ) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
    return normalize_mcp_result(result)


def normalize_mcp_result(result) -> dict[str, Any]:
    """标准化MCP调用结果(模块级公开,供 dataops_prod_adapter 复用).

    官方MCP返回格式:
    - CallToolResult with content: [TextContent(text='{"key": "value"}')]
    - 需要解析JSON字符串

    Returns:
        {
            "success": bool,
            "data": dict|Any,  # 解析后的数据
            "error": str        # 如果isError=True
        }
    """

    # 检查是否错误
    if hasattr(result, "isError") and result.isError:
        content = getattr(result, "content", None)
        error_msg = _extract_text_content(content) or "MCP工具调用失败"
        return {"success": False, "error": error_msg}

    # 尝试解析content
    content = getattr(result, "content", None)
    if content:
        text = _extract_text_content(content)
        if text:
            try:
                parsed = json.loads(text)
                return {"success": True, "data": parsed}
            except json.JSONDecodeError:
                # 非JSON格式,直接返回文本
                return {"success": True, "data": text}

    # 兜底: 尝试model_dump
    if hasattr(result, "model_dump"):
        try:
            dumped = result.model_dump()
            return {"success": True, "data": dumped}
        except Exception:
            pass

    return {"success": True, "data": str(result)}


def _extract_text_content(content) -> str | None:
    """从content中提取TextContent的文本"""
    from mcp.types import TextContent

    if not content:
        return None

    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, TextContent):
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(parts) if parts else None

    if isinstance(content, TextContent):
        return getattr(content, "text", None)

    return str(content) if content else None


_ENV_CONFIG = {
    "DATAOPS_ENV": "environment",
    "DATAOPS_EXEC_USER": "exec_user",
    "DATAOPS_TABLE_SUFFIX": "table_suffix",
    "DATAOPS_DISABLE_TEMP_CLEANUP": "disable_temp_cleanup",
    "DATAOPS_ENABLE_DROP_BEFORE_CREATE": "drop_before_create",
}


def getenv(name: str, default: str = "") -> str:
    """Read invocation-local plugin overrides without changing process environment."""
    value = (settings.get() or {}).get(_ENV_CONFIG.get(name, ""))
    return os.environ.get(name, default) if value is None else str(value)


async def invoke_model_text(model, messages) -> str:
    response = await collect_model_stream(model, messages)
    return response.content if isinstance(response.content, str) else response.text


class IntegrationClient:
    """Async client for the self-hosted DataOps MCP wire protocol."""

    def __init__(self, config: dict):
        url = config.get("integration_url") or os.environ.get("DATAOPS_MCP_URL", "")
        self.config = config
        self.url = url

    async def _call(self, name: str, arguments: dict) -> dict:
        result = await call_mcp(name, arguments, integration=True, config=self.config)
        if not result.get("success"):
            raise RuntimeError(result.get("error", "DataOps MCP call failed"))
        data = result.get("data")
        if not isinstance(data, dict):
            raise ValueError("DataOps MCP returned a non-object result")
        return data

    async def submit_job(self, *, resource_id, command, task_type, timeout_sec):
        return await self._call(
            "submit_job",
            {
                "envelope": {
                    "command": command,
                    "task_type": task_type,
                    "timeout_sec": timeout_sec,
                },
                "allocation": {"resource_id": resource_id},
            },
        )

    async def poll(self, *, job_id):
        return await self._call("poll_job", {"job_id": job_id})

    async def collect(self, *, job_id):
        return await self._call("collect_job", {"job_id": job_id})

    async def cancel(self, *, job_id):
        return await self._call("cancel_job", {"job_id": job_id})


def dataops_options(context: ModelContext | None) -> dict:
    return context.get_plugin_config("de-agent").get("dataops", {}) if context is not None else {}


@contextmanager
def dataops_settings(context: ModelContext):
    """Scope transport/environment overrides while passing the native context unchanged."""
    token = settings.set(dataops_options(context))
    try:
        yield context
    finally:
        settings.reset(token)
