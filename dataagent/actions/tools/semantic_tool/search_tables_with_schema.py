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
"""Semantic-service table and column search functionality.

Provides semantic search capabilities for discovering relevant database tables
and columns based on business-oriented keywords.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox
from dataagent.actions.tools.semantic_tool.get_table_desc import get_table_description
from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient
from dataagent.utils.constants import (
    DEFAULT_SEMANTIC_SERVICE_TABLE_LIST_LIMIT,
    DEFAULT_SEMANTIC_SERVICE_TYPENAME_SEARCH_TOP_K,
)

# ============================================================
# 工具主函数
# ============================================================


def search_tables_and_columns(keywords: list[str], top_k: int, *, _tool_context: ToolExecutionContext) -> dict:
    """Search for relevant database tables and columns by semantic keywords.

    Use this tool when you need to discover which tables and columns are
    related to certain business concepts or field names. It performs a
    semantic search against semantic-service and returns matching tables (with
    descriptions) and columns (with descriptions, data types, and sample
    values).

    Args:
        keywords: Business-oriented search terms, e.g. ["用户", "点击率", "app_id"].
        top_k: Maximum number of results per keyword (default: 10).

    Returns:
        dict with keys ``original_msg``, ``frontend_msg``, ``data``.
        ``data`` contains ``tables`` (list of table dicts) and ``columns``
        (list of column dicts with description, value_type, from_table).
    """
    if not keywords:
        return _fmt("未提供关键词，跳过元数据搜索。", "未提供关键词。", {"tables": [], "columns": []})

    dbs = _db_ids(_tool_context)
    if not dbs:
        return _fmt("DATABASE.db_id 未配置。", "数据库未配置。", {})

    client = SemanticServiceClient.from_config(_tool_context.config_manager)
    kw_str = ", ".join(keywords)

    per_db: dict[str, dict] = {}
    all_preview_lines: list[str] = []
    all_detail_lines: list[str] = []

    logger.info(f"[search_tables_and_columns] 开始处理，关键词: {keywords}, top_k: {top_k}")
    logger.info(f"[search_tables_and_columns] 配置的数据库列表: {dbs}")

    for db in dbs:
        logger.info(f"[search_tables_and_columns] 正在处理数据库: {db}")
        table_meta: dict[str, dict] = {}
        table_items = client.get_table_list(db, limit=DEFAULT_SEMANTIC_SERVICE_TABLE_LIST_LIMIT)
        for item in table_items:
            table_meta.update(item)
        logger.info(f"[search_tables_and_columns] 数据库 {db} 的表列表获取完成，共 {len(table_meta)} 张表")

        hit_tables: dict[str, dict] = {}
        hit_columns: list[dict] = []
        column_search_items = client.semantic_search_columns(db, keywords, top_k)
        for item in column_search_items:
            payload = next(iter(item.values()))
            for entry in payload.get("column_name_search", []):
                dtc = next(iter(entry))
                d, t, c = dtc.split(".")
                dt = f"{d}.{t}"
                if dt not in hit_tables:
                    meta = table_meta.get(dt, {})
                    hit_tables[dt] = {
                        "name": dt,
                        "description": meta.get("table_description_enhanced") or meta.get("table_description", ""),
                    }
                # 收集列详细信息
                entry_data = next(iter(entry.values()))
                hit_columns.append(
                    {
                        "name": dtc,
                        "column": c,
                        "from_table": dt,
                        "description": entry_data.get("description", ""),
                        "value_type": entry_data.get("value_type", ""),
                    }
                )

        logger.info(
            f"[search_tables_and_columns] 数据库 {db} 语义搜索完成，找到 {len(hit_tables)} 张相关表、{len(hit_columns)} 个相关列"
        )

        per_db[db] = {"tables": list(hit_tables.values()), "columns": hit_columns}

        db_summary = (
            f"[{db}] 关键词: [{kw_str}]  →  找到 {len(hit_tables)} 张相关表、{len(hit_columns)} 个语义命中字段。"
        )
        preview_lines: list[str] = [db_summary]
        tables_out = list(hit_tables.values())
        if tables_out:
            preview_lines.append("  相关表 (前5):" if len(tables_out) > 5 else "  相关表:")
            for table in tables_out[:5]:
                preview_lines.append(f"    - {table['name']}: {table.get('description', '')}")
            if len(tables_out) > 5:
                preview_lines.append(f"    … 还有 {len(tables_out) - 5} 张表")

        all_preview_lines.extend(preview_lines)
        all_detail_lines.append(db_summary)
        all_detail_lines.append(_pretty_tables_columns(tables_out, []))

    msg = "\n".join(all_preview_lines)
    detail = "\n".join(all_detail_lines)

    # 转换为 tables_with_columns 格式并保存到 .metric_dir
    tables_with_columns = _convert_to_tables_with_columns(per_db)
    logger.info(f"[search_tables_and_columns] 转换后的 tables_with_columns 包含 {len(tables_with_columns)} 张表")
    try:
        output_path, current_time = _get_workspace_path()
        logger.info(f"[search_tables_and_columns] 获取路径成功: output_path={output_path}, current_time={current_time}")
        file_path = output_path / f"output_semantic_service_tables_with_columns_{current_time}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(tables_with_columns, f, ensure_ascii=False, indent=2)
        logger.info(f"[search_tables_and_columns] 已保存文件: {file_path}, 包含 {len(tables_with_columns)} 张表")
    except Exception as e:
        logger.error(f"[search_tables_and_columns] 保存文件失败: {e}")

    return _fmt(detail, msg, {"per_db": per_db, "tables_with_columns": tables_with_columns})


def search_tables_with_semantic_retrieve(*, _tool_context: ToolExecutionContext) -> dict:
    """基于当前运行上下文里的原始用户 query 查找相关的表。

    这个工具不需要任何业务参数。函数会自动读取主 Agent 当前轮次的原始用户 query，
    并将该 query 传给 semantic-service 的 ``semantic/retrieve`` 接口。

    Returns:
        dict with ``data``，返回与原始用户 query 相关的表及其描述。
    """

    from dataagent.utils.info_utils import get_current_query

    # 手动替换成完整的原始用户 query
    query = get_current_query(_tool_context.runtime)
    if query is None:
        raise ValueError("original user query is required for semantic retrieve")

    client = SemanticServiceClient.from_config(_tool_context.config_manager)
    result = client.semantic_search_tables(query)
    _save_semantic_retrieve_diagnostic(result, query)

    recalled_tables = []
    res = f"匹配原始query的表如下：（query为 {query}）"
    data_access_plan = result.get("dataAccessPlan", {}) if isinstance(result, dict) else {}
    if isinstance(data_access_plan, dict):
        tables = data_access_plan.get("tables", [])
        for table in tables:
            res += "\n"
            res += f"{table.get('db', '')}.{table.get('table', '')} 描述：{table.get('description', '')}"
            recalled_tables.append(f"{table.get('db', '')}.{table.get('table', '')}")

    # 保存召回的 tables 到 .metric_dir 目录
    tables_columns = {table: [] for table in recalled_tables}
    tables_with_columns = _attach_table_descriptions(tables_columns, client)
    output_path, current_time = _get_workspace_path()
    _save_tables_with_columns_to_json(
        tables_with_columns, "output_search_tables_with_semantic_retrieve", output_path, current_time
    )

    # 保存 summary 到 .metric_dir 目录
    out_path, current_time = _get_workspace_path()
    summary_path = out_path / f"output_search_tables_with_retrieve_summary_{current_time}.txt"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(res)

    return {
        "original_msg": res,
        "frontend_msg": res,
        "data": res,
    }


def search_tables_with_typename(keywords: str, *, _tool_context: ToolExecutionContext):
    """基于给定的一个或多个关键字查找相关的表。

    函数会用关键字和不同类型检索相关表，再将每种类型的查表结果进行合并。

    Args:
        keywords（str）: 一个或多个关键字，多个关键字之间以空格分割。
    """
    client = SemanticServiceClient.from_config(_tool_context.config_manager)

    topk = DEFAULT_SEMANTIC_SERVICE_TYPENAME_SEARCH_TOP_K
    keyword_list = keywords.split()
    data_table_result = _fulltext_search_with_typename(keyword_list, "data_table", client, topk)
    data_column_result = _fulltext_search_with_typename(keyword_list, "data_column", client, topk)
    metric_instance_result = _fulltext_search_with_typename(keyword_list, "metric_instance", client, topk)

    metric_instance_entities = metric_instance_result.get("fullTextResult", [])
    data_column_entities = data_column_result.get("fullTextResult", [])
    data_table_entities = data_table_result.get("fullTextResult", [])

    metric_instance_qn_list = []
    for entity in metric_instance_entities:
        metric_instance_qn_list.append(entity["entity"]["attributes"]["qualified_name"])

    data_column_qn_list = []
    for entity in data_column_entities:
        data_column_qn_list.append(entity["entity"]["attributes"]["qualified_name"])

    data_table_qn_list = []
    for entity in data_table_entities:
        data_table_qn_list.append(entity["entity"]["attributes"]["qualified_name"])

    data_table_from_type_table = [table_qn.split("@")[0] for table_qn in data_table_qn_list]
    data_table_from_type_column = [column_qn.split("@")[0].rsplit(".", 1)[0] for column_qn in data_column_qn_list]

    _, metric_instance_table_list = _extract_columns_and_tables_from_metric(metric_instance_qn_list, client)

    merged_table_set = set()
    merged_table_set.update(data_table_from_type_table, data_table_from_type_column, metric_instance_table_list)

    tables_columns = {table: [] for table in merged_table_set}
    tables_with_columns = _attach_table_descriptions(tables_columns, client)

    res = f"匹配关键字的表如下：（关键字为 {keywords}）"
    for table_key, table_dict in tables_with_columns.items():
        table_name = table_key
        table_desc = table_dict["table_description"]
        res += "\n"
        res += f"{table_name} 描述：{table_desc}"

    # 保存 tables_with_3type及合并结果 到 .metric_dir 目录
    tables_with_3type = {}
    tables_with_3type["data_table_result"] = data_table_qn_list
    tables_with_3type["data_column_result"] = data_column_qn_list
    tables_with_3type["metric_instance_result"] = metric_instance_qn_list
    tables_with_3type["merged_table_set"] = list(merged_table_set)
    output_path, current_time = _get_workspace_path()
    _save_tables_with_columns_to_json(
        tables_with_3type, "output_search_tables_with_3type_result", output_path, current_time
    )

    # 保存 tables_with_columns 到 .metric_dir 目录
    output_path, current_time = _get_workspace_path()
    _save_tables_with_columns_to_json(tables_with_columns, "output_search_tables_with_type", output_path, current_time)

    # 保存 summary 到 .metric_dir 目录
    output_path, current_time = _get_workspace_path()
    with open(output_path / f"output_search_tables_with_type_summary_{current_time}.txt", "w", encoding="utf-8") as f:
        f.write(res)

    return {
        "original_msg": res,
        "frontend_msg": res,
        "data": res,
    }


def get_table_schema(
    table_name: str,
    *,
    _tool_context: ToolExecutionContext,
) -> dict:
    """Get the full column schema of a specific table from semantic-service.

    Use this tool when you already know the table name (in ``db.table``
    format) and need its complete column list with descriptions and types.

    Args:
        table_name: Fully-qualified table name, e.g. ``"mydb.user_action"``.

    Returns:
        dict with ``data.columns`` — a list of column dicts
        (name, description, value_type).
    """
    if not table_name:
        return _fmt("未提供表名。", "未提供表名。", {})

    client = SemanticServiceClient.from_config(_tool_context.config_manager)
    cols_raw = client.get_table_columns_info(table_name, limit=1000)

    columns: list[dict] = []
    for dtc, meta in cols_raw.items():
        _, _, c = dtc.split(".")
        columns.append(
            {
                "name": c,
                "full_name": dtc,
                "description": meta.get("column_short_description", ""),
                "value_type": meta.get("value_type", ""),
            }
        )

    summary = f"表 {table_name} 共 {len(columns)} 个字段。"
    lines = [f"  - {col['name']} ({col['value_type']}): {col['description']}" for col in columns]
    detail = summary + "\n" + "\n".join(lines)

    preview_lines: list[str] = [summary]
    if columns:
        preview_lines.append("字段 (前5):" if len(columns) > 5 else "字段:")
        for col in columns[:5]:
            preview_lines.append(f"  - {col['name']} ({col['value_type']}): {col['description']}")
        if len(columns) > 5:
            preview_lines.append(f"  … 还有 {len(columns) - 5} 个字段")
    msg = "\n".join(preview_lines)

    # 获取表描述
    table_description = ""
    try:
        table_qualified_name = f"{table_name}@hive"
        table_description = get_table_description(table_qualified_name, client)
    except Exception as e:
        logger.warning(f"[get_table_schema] 获取表描述失败: {e}")

    # 构建与 tables_with_columns 格式一致的数据并保存到 .metric_dir
    schema_data = {
        "table_name": table_name,
        "table_description": table_description,
        "columns": [
            {
                "column_name": col.get("name", ""),
                "column_description": col.get("description", ""),
                "column_type": col.get("value_type", ""),
            }
            for col in columns
        ],
    }
    logger.info(f"[get_table_schema] 构建保存数据完成，表: {table_name}, 包含 {len(columns)} 个字段")
    try:
        output_path, current_time = _get_workspace_path()
        logger.info(f"[get_table_schema] 获取路径成功: output_path={output_path}, current_time={current_time}")
        file_path = output_path / f"output_get_table_schema_{current_time}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(schema_data, f, ensure_ascii=False, indent=2)
        logger.info(f"[get_table_schema] 已保存文件: {file_path}, 包含 {len(columns)} 个字段")
    except Exception as e:
        logger.error(f"[get_table_schema] 保存文件失败: {e}")

    return _fmt(detail, msg, {"table": table_name, "columns": columns})


# ============================================================
# 工具辅助函数
# ============================================================


def _fmt(original: str, frontend: str, data: Any) -> dict:
    return {"original_msg": original, "frontend_msg": frontend, "data": data}


def _db_ids(_tool_context: ToolExecutionContext) -> list[str]:
    """Return configured database id(s) as a list.

    Supports both single string and list forms in config:
        db_id: "mydb"          -> ["mydb"]
        db_id: ["db1", "db2"]  -> ["db1", "db2"]
    """
    raw = _tool_context.config_manager.get("DATABASE.db_id", "")
    if isinstance(raw, list):
        return [s for s in raw if s and str(s).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _pretty_tables_columns(tables: list[dict], columns: list[dict]) -> str:
    parts: list[str] = []
    if tables:
        parts.append("Tables:")
        for t in tables:
            parts.append(f"  - {t['name']}: {t.get('description', '')}")
    if columns:
        parts.append("Columns (semantic hits marked with *):")
        for c in columns:
            marker = "*" if c.get("is_semantic_hit") else " "
            parts.append(f"  {marker} {c['name']} ({c.get('value_type', '')}): {c.get('description', '')}")
    return "\n".join(parts)


def _convert_to_tables_with_columns(per_db: dict[str, dict]) -> dict[str, dict]:
    """将 per_db 格式的数据转换为 tables_with_columns 格式。

    Args:
        per_db: 包含各数据库表列信息的字典，格式为:
            {
                "db_name": {
                    "tables": [{"name": "表名", "description": "描述"}, ...],
                    "columns": [{"name": "完整列名", "column": "列名", "from_table": "表名",
                                 "description": "描述", "value_type": "类型", ...}, ...]
                },
                ...
            }

    Returns:
        dict: tables_with_columns 格式的字典，格式为:
            {
                "表名": {
                    "table_name": "表名",
                    "table_description": "描述",
                    "columns": [
                        {"column_name": "列名", "column_description": "描述", "column_type": "类型"},
                        ...
                    ]
                },
                ...
            }
    """
    tables_with_columns: dict[str, dict] = {}

    for db_data in per_db.values():
        tables = db_data.get("tables", [])
        columns = db_data.get("columns", [])

        # 构建表到列的映射
        table_columns: dict[str, list[dict]] = {}
        for col in columns:
            table_name = col.get("from_table", "")
            if not table_name:
                continue
            if table_name not in table_columns:
                table_columns[table_name] = []
            table_columns[table_name].append(
                {
                    "column_name": col.get("column", ""),
                    "column_description": col.get("description", ""),
                    "column_type": col.get("value_type", ""),
                }
            )

        # 构建 tables_with_columns 格式
        for table in tables:
            table_name = table.get("name", "")
            if table_name not in tables_with_columns:
                tables_with_columns[table_name] = {
                    "table_name": table_name,
                    "table_description": table.get("description", ""),
                    "columns": [],
                }
            # 追加该表的列信息
            if table_name in table_columns:
                tables_with_columns[table_name]["columns"].extend(table_columns[table_name])

    return tables_with_columns


def _get_workspace_path() -> tuple[Path, str]:
    """获取 .metric_dir 目录路径和时间戳。

    Returns:
        tuple: (output_path, current_time) - metric_dir 路径和时间戳
    """
    guard = get_current_sandbox()
    workspace_path = guard.workspace_root
    if workspace_path is None:
        raise ValueError("workspace_root is required to save metric search results")
    output_path = workspace_path / ".metric_dir"
    # 确保目录存在
    output_path.mkdir(parents=True, exist_ok=True)
    current_time = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    return output_path, current_time


def _get_semantic_path() -> tuple[Path, str]:
    """获取 .semantic 目录路径和时间戳。"""
    guard = get_current_sandbox()
    workspace_path = guard.workspace_root
    if workspace_path is None:
        raise ValueError("workspace_root is required to save semantic retrieve diagnostics")
    output_path = workspace_path / ".semantic"
    output_path.mkdir(parents=True, exist_ok=True)
    current_time = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    return output_path, current_time


def _save_semantic_retrieve_diagnostic(result: Any, query: str) -> Optional[Path]:  # noqa: UP045
    """Save semantic retrieve diagnostic payload when the service returns it."""
    if not isinstance(result, dict):
        return None

    diagnostic = result.get("diagnostic")
    if diagnostic is None:
        return None

    output_path, current_time = _get_semantic_path()
    file_path = output_path / f"semantic_retrieve_diagnostic_{current_time}.json"
    payload = {
        "tool": "search_tables_with_semantic_retrieve",
        "endpoint": "semantic/retrieve",
        "query": query,
        "created_at": datetime.now(UTC).isoformat(),
        "diagnostic": diagnostic,
    }
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"[search_tables_with_semantic_retrieve] 已保存 diagnostic: {file_path}")
    return file_path


def _fulltext_search_with_typename(
    keywords: list[str],
    typename: str,
    client: SemanticServiceClient,
    topk: int,
) -> dict:
    """根据关键词和不同类型，从语义服务 API 查询结果。

    Args:
        keywords: 关键词列表
        typename: 实体类型
        client: 统一语义服务客户端
        topk: 返回的记录条数

    Returns:
        dict: API返回的JSON结果
    """
    query_str = " ".join(keywords)
    return client.search_fulltext(query_str, type_name=typename, limit=topk, offset=0, exclude_deleted=False)


def _extract_columns_and_tables_from_metric(
    metric_qualified_names: list[str], client: SemanticServiceClient
) -> tuple[list[str], list[str]]:
    """
    根据 metric_instance qualified_name 提取指标列和指标表。

    函数会逐个查询 metric_instance 详情，从 relationshipAttributes.sourceColumns
    提取 data_column，从 relationshipAttributes.realizedTables 提取 data_table。

    Args:
        metric_qualified_names: metric_instance qualified_name 列表
        client: 统一语义服务客户端

    Returns:
        tuple[list[str], list[str]]: 指标列 qualified_name 列表、指标表名列表。
    """
    column_list = []
    table_list = []
    for metric_name in metric_qualified_names:
        metric_detail = client.get_entity_by_unique_attribute("metric_instance", "qualified_name", metric_name)

        # 提取列
        for column in metric_detail.get("entity", {}).get("relationshipAttributes", {}).get("sourceColumns", []):
            if column.get("typeName") == "data_column":
                column_list.append(column["qualifiedName"])
        column_list = list(dict.fromkeys(column_list))

        # 提取表
        for table in metric_detail.get("entity", {}).get("relationshipAttributes", {}).get("realizedTables", []):
            if table.get("typeName") == "data_table":
                # 去掉后缀 @hive
                table_name = table["qualifiedName"].split("@")[0]
                if table_name not in table_list:
                    table_list.append(table_name)
        table_list = list(dict.fromkeys(table_list))

    return column_list, table_list


def _attach_table_descriptions(
    tables_columns: dict,
    client: SemanticServiceClient,
    description_cache: dict[str, str] | None = None,
) -> dict:
    """
    为表列信息补充表描述，并转换成包含 description 和 columns 的结构。

    Args:
        tables_columns: 表列信息字典，结构为 {表名: [列信息列表]}
        client: 统一语义服务客户端
        description_cache: 表描述缓存，避免多个中间结果重复查询同一张表

    Returns:
        dict: {表名: {"table_name": 表名, "table_description": 表描述, "columns": [列信息列表]}}
    """
    result = {}
    cache = description_cache if description_cache is not None else {}
    for table_name, columns in tables_columns.items():
        table_qualified_name = f"{table_name}@hive"
        if table_qualified_name not in cache:
            cache[table_qualified_name] = get_table_description(table_qualified_name, client)
        result[table_name] = {
            "table_name": table_name,
            "table_description": cache[table_qualified_name],
            "columns": columns,
        }
    return result


def _save_tables_with_columns_to_json(
    tables_with_columns: dict, file_prefix: str, output_path: Path, current_time: str
):
    """将 tables_with_columns 格式的数据保存为 JSON 文件。

    Args:
        tables_with_columns: 表列信息字典
        file_prefix: 文件名前缀
        output_path: 输出目录路径
        current_time: 时间戳字符串
    """
    file_path = output_path / f"{file_prefix}_{current_time}.json"
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(tables_with_columns, f, ensure_ascii=False, indent=2)
