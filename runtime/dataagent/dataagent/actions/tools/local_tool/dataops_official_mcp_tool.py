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
"""官方DataOps MCP直接接入 - 执行SQL和查询结果

提供2个工具:
1. dataops_execute_sql - 执行SQL查询
2. dataops_get_query_result - 查询SQL任务执行结果

使用方式 (YAML配置):
    TOOLS:
      local_functions:
        - module: "dataagent.actions.tools.local_tool.dataops_official_mcp_tool"
          function: "dataops_execute_sql"
        - module: "dataagent.actions.tools.local_tool.dataops_official_mcp_tool"
          function: "dataops_get_query_result"

环境变量:
    DATAOPS_AUTHORIZATION=Bearer <your_token>
"""

from __future__ import annotations

import json
import os
from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.mcp import MCPClientWrapper, MCPServerConfig
from dataagent.utils.log import logger

# ============================================================================
# MCP连接配置
# ============================================================================


def _get_mcp_config() -> MCPServerConfig:
    """获取MCP服务器配置

    注意: env 变量在每次调用时读取,这样可以热加载新的 DATAOPS_MCP_URL / DATAOPS_AUTHORIZATION
    (避免模块级常量在 import 时一次性绑定,配置变更需要重启进程的问题)
    """
    mcp_url = os.environ.get("DATAOPS_MCP_URL", "http://7.185.25.122:8000/dataops/mcp").strip()
    auth = os.environ.get("DATAOPS_AUTHORIZATION", "").strip()
    timeout_raw = os.environ.get("DATAOPS_MCP_TIMEOUT", "300").strip()
    try:
        timeout = float(timeout_raw)
    except ValueError:
        timeout = 300.0

    logger.debug(f"[dataops_mcp_config] url={mcp_url!r} auth={'set' if auth else 'EMPTY'} timeout={timeout}")

    return MCPServerConfig(
        server_id="dataops-official",
        transport_type="streamable_http",
        config={
            "url": mcp_url,
            "headers": {"Authorization": auth} if auth else {},
            "timeout": timeout,
        },
        category="mcp",
        description="官方DataOps MCP服务",
    )


def create_mcp_client() -> MCPClientWrapper:
    """创建MCP客户端(模块级公开,供 dataops_prod_adapter 复用)."""
    config = _get_mcp_config()
    return MCPClientWrapper(config)


# ============================================================================
# 工具实现
# ============================================================================


async def dataops_execute_sql(
    sql: str,
    connectDatabase: str = "Clickhouse",
    serviceInstance: str | None = None,
    fiClusterName: str | None = None,
    sourceType: str = "Hive",
    connectName: str | None = None,
    configParams: list | None = None,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """在DataOps平台上执行SQL查询

    官方MCP工具: dataops_execute_sql

    执行后端选择规则：
    - 若 SQL 是查询语句，优先使用 Clickhouse
    - 若 SQL 不是查询语句，优先使用 Spark_Sync
    - 若 Clickhouse 不可用自动降级为 Spark_Sync；若 Spark_Sync 失败，转为 Spark

    Args:
        sql: SQL查询语句 (例如: 'SELECT * FROM users WHERE id = 1')
        connectDatabase: 连接的数据库执行引擎，可选 'Clickhouse', 'Spark_Sync', 'Spark'
        serviceInstance: 用户明确指定的子数据域名称。仅当用户明确提到子数据域时填写
        fiClusterName: 用户明确指定的集群名称。仅当用户明确提到集群时填写
        sourceType: 数据源类型，可选 'Hive', 'Iceberg', 'Clickhouse'。查询Iceberg表时传'Iceberg'，connectDatabase传'Spark'
        connectName: 连接名称。当sourceType为Clickhouse时为必填参数
        configParams: 自定义执行参数配置列表。仅当用户明确要求修改执行参数时使用

    Returns:
        {
            "success": bool,
            "data": {
                "queryJobId": int,  # 任务ID
                "sql": str,
                "connectDatabase": str,
                "sourceType": str,
                "engineSwitch": str  # 可选，降级时返回
            },
            "error": str  # 失败时返回
        }
    """
    client = create_mcp_client()
    try:
        arguments: dict[str, Any] = {
            "sql": sql,
            "connectDatabase": connectDatabase,
            "sourceType": sourceType,
        }
        # 添加可选参数
        if serviceInstance is not None:
            arguments["serviceInstance"] = serviceInstance
        if fiClusterName is not None:
            arguments["fiClusterName"] = fiClusterName
        if connectName is not None:
            arguments["connectName"] = connectName
        if configParams is not None:
            arguments["configParams"] = configParams

        logger.debug(f"[dataops_execute_sql] 调用MCP, sql长度={len(sql)}")
        result = await client._execute_with_connection(
            lambda session: session.call_tool(name="dataops_execute_sql", arguments=arguments)
        )
        return normalize_mcp_result(result)
    except Exception as e:
        logger.error(f"[dataops_execute_sql] MCP调用失败: {e}")
        return {"success": False, "error": str(e)}


async def dataops_get_query_result(
    queryJobId: int,
    serviceInstance: str | None = None,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """根据queryJobId查询SQL任务的执行结果

    官方MCP工具: dataops_get_query_result

    状态值：Running（执行中）、Failed（失败，含错误日志）、Success（成功，含结果数据）

    Args:
        queryJobId: 任务ID (queryJobId)
        serviceInstance: 用户明确指定的子数据域名称。仅当用户明确提到子数据域时填写

    Returns:
        执行中（Running）:
        {
            "status": "Running",
            "queryJobId": int
        }

        失败（Failed）:
        {
            "status": "Failed",
            "queryJobId": int,
            "errorLog": "错误日志内容..."
        }

        成功（Success，含结果数据）:
        {
            "status": "Success",
            "queryJobId": int,
            "data": {
                "headers": ["id", "name", "age"],
                "rows": [[1, "Alice", 25], [2, "Bob", 30]]
            }
        }

        成功但无结果数据（DDL语句如CREATE TABLE）:
        {
            "status": "Success",
            "queryJobId": int,
            "data": null,
            "message": "任务执行成功，该语句无查询结果数据（通常为DDL语句，如CREATE TABLE）"
        }
    """
    client = create_mcp_client()
    try:
        arguments: dict[str, Any] = {"queryJobId": queryJobId}
        if serviceInstance is not None:
            arguments["serviceInstance"] = serviceInstance

        logger.debug(f"[dataops_get_query_result] 查询任务状态, jobId={queryJobId}")
        result = await client._execute_with_connection(
            lambda session: session.call_tool(name="dataops_get_query_result", arguments=arguments)
        )
        return normalize_mcp_result(result)
    except Exception as e:
        logger.error(f"[dataops_get_query_result] MCP调用失败: {e}")
        return {"success": False, "error": str(e)}


# ============================================================================
# 辅助函数
# ============================================================================


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
