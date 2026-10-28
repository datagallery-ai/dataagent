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
"""Production DataOps adapter (扁平封装,不走 resource-coordinator 接口).

封装官方 DataOps MCP 的两步式接口:
    - dataops_execute_sql()       提交 SQL,获取 queryJobId
    - dataops_get_query_result()  轮询直到 terminal 状态

状态映射(官方 -> 内部统一状态):
    Running  -> running    执行中
    Success  -> completed  成功
    Failed   -> failed    失败
    网络异常 -> error      提交/查询失败

日志获取: 官方 MCP 在 Failed 响应中直接返回 errorLog,无需二次 OBS 拉取。
结果数据: 官方 MCP 在 Success 响应中直接返回 data: {headers, rows},无需 OBS 下载。
"""

from __future__ import annotations

from typing import Any

from dataagent.utils.log import logger

# 官方 status -> 内部统一状态
_STATUS_MAP = {
    "Running": "running",
    "Success": "completed",
    "Failed": "failed",
}

# ``error`` 字段回显给 LLM 时的长度上限(见 ``_summarize_error_log``)。
_ERROR_SUMMARY_MAX_CHARS = 500


def _summarize_error_log(error_log: str) -> str:
    """把可能非常大的 Spark 错误日志压成一句短摘要,用于 ``error`` 字段。

    只取日志头部:异常类型/消息和栈顶在这里,也就是根因。完整日志仍保留在
    ``errorLog`` 字段里(下游会再按 agent 边界收敛),这里只保证 ``error``
    不会变成几百 KB 的整份日志。
    """
    if not error_log:
        return ""
    text = error_log.strip()
    if len(text) <= _ERROR_SUMMARY_MAX_CHARS:
        return text
    return text[:_ERROR_SUMMARY_MAX_CHARS] + " ...(truncated)"


# ============================================================================
# 核心辅助函数
# ============================================================================


async def _call_official_mcp(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """调用官方 DataOps MCP 工具,统一做归一化处理.

    内部复用 dataops_official_mcp_tool 的 normalize / create_client(模块级)。
    """
    from dataagent.actions.tools.local_tool.dataops_official_mcp_tool import (
        create_mcp_client,
        normalize_mcp_result,
    )

    logger.debug(f"[dataops_prod_adapter] calling MCP tool={tool_name!r} args_keys={list(arguments.keys())}")
    if "sql" in arguments:
        logger.debug(f"[dataops_prod_adapter] MCP sql=\n{arguments['sql']}\n")
    client = create_mcp_client()
    try:
        result = await client._execute_with_connection(
            lambda session: session.call_tool(name=tool_name, arguments=arguments)
        )
        return normalize_mcp_result(result)
    except Exception as exc:
        # MCP client (anyio-based) may raise various exceptions including task-group errors
        # from streamable_http. Catch all so nothing unhandled bubbles up.
        return {"success": False, "error": str(exc)}


# ============================================================================
# 生产环境执行器(扁平结构,不套 resource-coordinator 壳)
# ============================================================================


async def prod_execute_sql(sql: str) -> dict[str, Any]:
    """调用官方 MCP dataops_execute_sql,返回 {success, job_id, raw_data}。

    Returns:
        {
            "success": bool,
            "job_id": str | None,    # 官方 queryJobId,字符串形式
            "raw_data": dict | None,
            "error": str | None,
        }
    """
    result = await _call_official_mcp(
        "dataops_execute_sql",
        {
            "sql": sql,
            "connectDatabase": "Spark_Sync",  # 默认走 Clickhouse,官方 MCP 会自动降级
            "sourceType": "Hive",
        },
    )
    logger.debug(f"[dataops_prod_adapter] prod_execute_sql result success={result.get('success')} raw={result}")
    if not result.get("success"):
        return {
            "success": False,
            "job_id": None,
            "raw_data": None,
            "error": result.get("error", "execute_sql failed"),
        }
    data = result.get("data") or {}
    job_id = data.get("queryJobId")
    logger.debug(f"[dataops_prod_adapter] prod_execute_sql extracted job_id={job_id!r} raw_data={data}")
    return {
        "success": job_id is not None,
        "job_id": str(job_id) if job_id is not None else None,
        "raw_data": data,
        "error": None if job_id else "no queryJobId in response",
    }


async def prod_get_query_result(job_id: str) -> dict[str, Any]:
    """调用官方 MCP dataops_get_query_result,返回内部统一状态。

    Returns:
        {
            "status": "running" | "completed" | "failed" | "error",
            "raw": dict,           # 官方原始响应
            "error": str | None,  # 仅在 status=error 时有值
        }
    """
    result = await _call_official_mcp(
        "dataops_get_query_result",
        {"queryJobId": int(job_id)},
    )
    logger.debug(f"[dataops_prod_adapter] prod_get_query_result({job_id}) raw_result={result}")
    if not result.get("success"):
        return {"status": "error", "raw": {}, "error": result.get("error", "poll failed")}
    data = result.get("data") or {}
    official_status = str(data.get("status") or "").strip()
    internal_status = _STATUS_MAP.get(official_status, "error")
    logger.debug(f"[dataops_prod_adapter] prod_get_query_result({job_id}) official_status={official_status!r} internal_status={internal_status!r}")
    return {"status": internal_status, "raw": data, "error": None}


async def prod_collect_result(job_id: str) -> dict[str, Any]:
    """获取生产任务的最终结果(terminal result)。

    成功时: 从 raw.data 取 {headers, rows},转换为 list[dict] 供后置验证使用。
    失败时: 从 raw.errorLog 取错误信息,不需要 OBS 二次拉取。

    Returns:
        {
            "status": "completed" | "failed" | "error",
            "job_id": str,
            "data": list[dict] | None,     # 成功时为转换后的行数据
            "data_meta": None,
            "error": str | None,
            "errorLog": str | None,        # 失败时直接含 inline errorLog
            "raw_result": dict,
        }
    """
    polled = await prod_get_query_result(job_id)
    raw = polled.get("raw") or {}

    if polled["status"] == "completed":
        # 将官方返回转换为 list[dict],供后置验证的 _parse_count_from_collect 使用。
        # 兼容多种官方返回格式:
        #   - {"data": {"headers": [...], "rows": [[...]]}}   ← 文档示例
        #   - {"data": [...]}                                  ← 直接 list
        #   - {"data": 5} / {"data": "5"}                      ← 标量(罕见)
        #   - {"data": null} / {"data": {}}                    ← 空(常见于 DDL)
        payload = raw.get("data")
        headers: list = []
        rows: list = []
        rows_as_dicts: list[dict] = []

        if isinstance(payload, dict):
            # 官方文档示例是 {headers, rows}，但实际生产返回的是
            # {queryResultColumns: [{title, width}], data: [[...]]}
            if "queryResultColumns" in payload and "data" in payload:
                # 真实生产格式: 从 queryResultColumns 提取列名
                cols = payload.get("queryResultColumns") or []
                headers = [c.get("title", "") if isinstance(c, dict) else str(c) for c in cols]
                rows = payload.get("data") or []
                rows_as_dicts = [dict(zip(headers, row, strict=True)) for row in rows] if headers else []
            elif "headers" in payload and "rows" in payload:
                # 文档示例格式(保留兼容)
                headers = payload.get("headers") or []
                rows = payload.get("rows") or []
                rows_as_dicts = [dict(zip(headers, row, strict=True)) for row in rows] if headers else []
            else:
                rows_as_dicts = []
        elif isinstance(payload, list):
            # 官方有时直接返回 list,如 [5] 或 [{"k": "v"}]
            if payload and isinstance(payload[0], dict):
                rows_as_dicts = payload
            elif payload:
                # 标量 list,如 [5]
                rows_as_dicts = [{"value": v} for v in payload]
        # else: payload 是 None/标量/空 dict → rows_as_dicts 保持 []

        logger.debug(
            f"[dataops_prod_adapter] prod_collect_result({job_id}) raw={raw}"
        )
        return {
            "status": "completed",
            "job_id": job_id,
            "data": rows_as_dicts,
            "data_meta": None,
            "error": None,
            "errorLog": None,
            "raw_result": raw,
        }

    if polled["status"] == "failed":
        error_log = raw.get("errorLog") or ""
        logger.debug(f"[dataops_prod_adapter] prod_collect_result({job_id}) raw={raw}")
        return {
            "status": "failed",
            "job_id": job_id,
            "data": None,
            "data_meta": None,
            # ``error`` 与 ``errorLog`` 同源,不要把整份 Spark 日志复制到
            # ``error`` 里(下游会把 ``error`` 当作一句错误摘要直接回显给
            # LLM)。这里只放一个短摘要,完整日志仍在 ``errorLog``。
            "error": _summarize_error_log(error_log) or "status=failed",
            "errorLog": error_log,
            "raw_result": raw,
        }

    # error
    return {
        "status": "error",
        "job_id": job_id,
        "data": None,
        "data_meta": None,
        "error": polled.get("error") or "unknown poll error",
        "errorLog": None,
        "raw_result": raw,
    }
