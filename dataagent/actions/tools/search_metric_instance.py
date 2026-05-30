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

import copy
import glob
import json
import os
from datetime import UTC, datetime
from typing import Any

import requests
from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox


# ============================================================
# 工具主函数
# ============================================================
def search_metric_instance(keywords: list[str], *, _tool_context: ToolExecutionContext):
    """
    根据关键词搜索元数据指标 metric_instance 并返回表和列信息

    Args:
        keywords (list[str]): 关键词列表，例如 ["用户", "app二级分类", "使用时长", "7天", "14天", "30天"]

    Returns:
        dict: {
            "original_msg": str,
            "frontend_msg": str,
            "data": {
                "output_tables_from_metric_instance_column_list": dict,
                "output_columns_from_tables_with_columns": dict,
                "output_tables_with_columns": dict
            }
        }
        - 包含原始消息、前端消息和数据，其中数据包含最终召回指标的表与列信息
    """
    cm = _tool_context.config_manager
    if cm is None:
        raise RuntimeError("search_metric_instance requires per-Agent ConfigManager in _tool_context.")

    # 语义感知增强-元数据增强模块 基础URL和认证
    base_url = cm.get("METAVISOR.metavisor_url")
    auth = ("admin", "admin")

    # Step 1: 根据 ”模型传入的 keywords”，粗召回筛选出与 keywords 相关的 “metric_instance、data_column、data_table”
    result_json = _coarse_recall_metric_instances(keywords, base_url, auth)

    # Step 2: 根据 “粗召回的 typeName”，筛选出 “metric_instance 的 qualifiedName”
    metric_instance_list = _filter_metric_instances_by_type(result_json, "metric_instance")

    # Step 3: 根据 “metric_instance 的 qualifiedName”，提取出 ”指标列的 qualifiedName” 和 “指标表的 qualifiedName”
    metric_instance_column_list, metric_instance_table_list = _extract_columns_and_tables_from_metric(
        metric_instance_list, base_url, auth
    )

    # Step 4: 根据 “指标列的 qualifiedName”，获取 “指标列详情“ 和 ”指标列所在表信息”
    tables_from_metric_instance_column_list = _get_column_details(metric_instance_column_list, base_url, auth)
    tables_with_columns = copy.deepcopy(tables_from_metric_instance_column_list)

    # Step 5: 根据 “指标列所在表的 qualifiedName”，获取 “指标列所在表的所有列信息”（扩充召回结果）
    tables_from_columns = list(tables_from_metric_instance_column_list.keys())
    columns_from_tables_with_columns = _get_table_all_columns(tables_from_columns, base_url, auth)

    # Step 6: 根据 “指标表的 qualifiedName”，获取 “指标表的所有列信息”（扩充召回结果）
    columns_from_metric_instance_table_list = _get_table_all_columns(metric_instance_table_list, base_url, auth)

    # Step 7（可选：如果需要扩充召回结果）: 合并 Step4 和 Step5
    # tables_with_columns为： _merge_table_columns(tables_with_columns, columns_from_tables_with_columns)

    # Step 8（可选：如果需要扩充召回结果）: 合并 Step6 和 Step7
    # tables_with_columns为：_merge_table_columns(tables_with_columns, columns_from_metric_instance_table_list)

    # Step 9: 生成 summary 并 保存结果
    summary = _generate_summary(
        keywords, len(result_json.get("fullTextResult", [])), len(metric_instance_list), tables_with_columns
    )

    guard = get_current_sandbox()
    workspace_path = guard.workspace_root
    if workspace_path is None:
        raise ValueError("workspace_root is required to save metric search results")
    output_path = workspace_path / "metric_dir"
    os.makedirs(output_path, exist_ok=True)
    current_time = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    logger.info(f"output_path_for_metric: {output_path}")

    _save_json_file(result_json, os.path.join(output_path, f"1_output_metric_coarse_recall_{current_time}.json"))
    _save_json_file(
        metric_instance_list, os.path.join(output_path, f"2_output_metric_instance_list_{current_time}.json")
    )
    _save_json_file(
        metric_instance_column_list,
        os.path.join(output_path, f"3_1_output_metric_instance_column_list_{current_time}.json"),
    )
    _save_json_file(
        metric_instance_table_list,
        os.path.join(output_path, f"3_2_output_metric_instance_table_list_{current_time}.json"),
    )
    _save_json_file(
        tables_from_metric_instance_column_list,
        os.path.join(output_path, f"4_output_tables_from_metric_instance_column_list_{current_time}.json"),
    )
    _save_json_file(
        columns_from_tables_with_columns,
        os.path.join(output_path, f"5_output_columns_from_tables_with_columns_{current_time}.json"),
    )
    _save_json_file(
        columns_from_metric_instance_table_list,
        os.path.join(output_path, f"6_output_columns_from_metric_instance_table_list_{current_time}.json"),
    )
    _save_json_file(tables_with_columns, os.path.join(output_path, f"output_tables_with_columns_{current_time}.json"))

    with open(os.path.join(output_path, f"output_metric_summary_{current_time}.txt"), "w", encoding="utf-8") as f:
        f.write(summary)

    # Step 10: 生成 schema 中间表示并保存到 nl2sql subagent 指定的目录
    _build_schema_ir_for_nl2sql(
        workspace_path=str(workspace_path),
        output_path=str(output_path),
        save_file=True,
        target_dir=cm.get("WORKSPACE.target_path"),
    )

    # Step 11: 返回结果
    original_msg = json.dumps(
        {
            "output_tables_with_columns": tables_with_columns,
            "output_tables_from_metric_instance_column_list": tables_from_metric_instance_column_list,
            "output_columns_from_tables_with_columns": columns_from_tables_with_columns,
        },
        ensure_ascii=False,
        indent=2,
    )

    return _fmt(
        original_msg,
        summary,
        {
            "output_tables_with_columns": tables_with_columns,
            "output_tables_from_metric_instance_column_list": tables_from_metric_instance_column_list,
            "output_columns_from_tables_with_columns": columns_from_tables_with_columns,
        },
    )


# ============================================================
# 工具辅助函数
# ============================================================


def _coarse_recall_metric_instances(keywords: list[str], base_url: str, auth: tuple) -> dict:
    """
    Step 1：根据关键词粗召回 metric_instance

    Args:
        keywords: 关键词列表
        base_url: API基础URL
        auth: 认证元组 (用户名, 密码)

    Returns:
        dict: API返回的JSON结果
    """
    query_str = " ".join(keywords)
    search_url = f"{base_url}/api/metaVisor/v3/search/fulltext"
    params = {"query": query_str, "limit": 100, "offset": 0, "excludeDeletedEntities": "true"}
    result = requests.get(search_url, params=params, auth=auth)
    return result.json()


def _filter_metric_instances_by_type(search_result: dict, target_type: str = "metric_instance") -> list[str]:
    """
    Step 2：从粗召回结果筛选指定类型的 qualifiedName

    Args:
        search_result: _coarse_recall_metric_instances 的返回结果
        target_type: 目标类型名称

    Returns:
        list[str]: qualifiedName 列表
    """
    return [
        entity["entity"]["attributes"]["qualifiedName"]
        for entity in search_result.get("fullTextResult", [])
        if entity.get("entity", {}).get("typeName") == target_type
    ]


def _extract_columns_and_tables_from_metric(
    metric_qualified_names: list[str], base_url: str, auth: tuple
) -> tuple[list[str], list[str]]:
    """
    Step 3：根据 metric qualifiedName 提取列和表的 qualifiedName

    Args:
        metric_qualified_names: metric_instance qualifiedName 列表
        base_url: API基础URL
        auth: 认证元组

    Returns:
        tuple: (列qualifiedName列表, 表qualifiedName列表)
    """
    column_list = []
    table_list = []
    for metric_name in metric_qualified_names:
        metric_detail_url = f"{base_url}/api/metaVisor/v3/entity/uniqueAttribute/type/metric_instance"
        metric_params = {"attr:qualifiedName": metric_name}
        metric_detail = requests.get(metric_detail_url, params=metric_params, auth=auth).json()

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


def _get_column_details(column_qualified_names: list[str], base_url: str, auth: tuple) -> dict:
    """
    Step 4：根据列 qualifiedName 获取列详情和所在表信息

    Args:
        column_qualified_names: 列 qualifiedName 列表
        base_url: API基础URL
        auth: 认证元组

    Returns:
        dict: {表名: [列信息列表]}
    """
    tables_columns = {}
    for column_qualified_name in column_qualified_names:
        column_detail_url = f"{base_url}/api/metaVisor/v3/entity/uniqueAttribute/type/data_column"
        column_params = {"attr:qualifiedName": column_qualified_name}
        column_detail = requests.get(column_detail_url, params=column_params, auth=auth).json()

        table_name = column_detail.get("entity", {}).get("attributes", {}).get("table_id")
        column = column_detail.get("entity", {}).get("attributes", {})
        column_kept = {
            "column_name": column.get("column_name_en", ""),
            "column_description": column.get("column_description", ""),
            "column_type": column.get("value_type", ""),
        }
        if table_name not in tables_columns:
            tables_columns[table_name] = []
        if column_kept not in tables_columns[table_name]:
            tables_columns[table_name].append(column_kept)

    return tables_columns


def _get_table_all_columns(table_names: list[str], base_url: str, auth: tuple) -> dict:
    """
    Step 5/6：根据表名获取表的所有列信息

    Args:
        table_names: 表名列表
        base_url: API基础URL
        auth: 认证元组

    Returns:
        dict: {表名: [列信息列表]}
    """
    tables_columns = {}
    for table_name in table_names:
        tables_columns[table_name] = []
        table_columns_url = f"{base_url}/api/metaVisor/v3/advanced-search/table-columns-info"
        params = {"tableName": table_name, "limit": 100}
        all_columns = requests.get(table_columns_url, params=params, auth=auth).json()

        tables_columns[table_name] = [
            {
                "column_name": column_name.split(".")[-1],
                "column_description": all_columns[column_name]["column_short_description"],
                "column_type": all_columns[column_name]["value_type"],
            }
            for column_name in all_columns
        ]

    return tables_columns


def _merge_table_columns(target: dict, source: dict) -> dict:
    """
    Step 7/8：将 source 中的表列信息合并到 target（扩充召回结果）

    Args:
        target: 目标字典 {表名: [列信息列表]}
        source: 源字典 {表名: [列信息列表]}

    Returns:
        dict: 合并后的字典
    """
    for table_name, columns in source.items():
        if table_name not in target:
            target[table_name] = columns
        else:
            for column in columns:
                if column not in target[table_name]:
                    target[table_name].append(column)
    return target


def _generate_summary(keywords: list[str], coarse_count: int, refine_count: int, tables_with_columns: dict) -> str:
    """
    生成检索摘要信息

    Args:
        keywords: 关键词列表
        coarse_count: 粗召回数量
        refine_count: 精筛后数量
        tables_with_columns: 表列信息字典

    Returns:
        str: 摘要字符串
    """
    column_cnt = sum(len(columns) for columns in tables_with_columns.values())

    summary = (
        f'检索Agent基础解析得到的关键词 "{keywords}",'
        f"粗召回候选 metric_instance {coarse_count} 个，"
        f"结构化精筛得到候选 metric_instance {refine_count} 个，"
        f"最终成功获取 {len(tables_with_columns)} 个表，{column_cnt} 个列，其中："
    )

    for table_name, columns in tables_with_columns.items():
        summary += f"\n- 表 {table_name} 召回了 {len(columns)} 个列"

    return summary


def _save_json_file(data: Any, file_path: str) -> None:
    """
    保存 metric_instance 搜索结果 JSON 数据到文件

    Args:
        data: 要保存的数据
        file_path: 文件路径
    """
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _build_schema_ir_for_nl2sql(
    workspace_path: str | None = None,
    output_path: str | None = None,
    save_file: bool = True,
    target_dir: str | None = None,
) -> dict[str, Any]:
    """
    汇总 metric_dir 结果目录中的表列信息，生成 schema 中间表示并保存到 nl2sql subagent 指定的目录。

    Args:
        workspace_path: 工作空间路径,默认为None
        output_path: 输出路径，默认为 None
        save_file: 是否保存结果到 schema_zhongduanyun_schemair.md，默认为 True
        target_dir: nl2sql subagent 目标目录，来自 ``WORKSPACE.target_path``。

    Returns:
        整合后的schema_ir字典，格式如下:
        {
            "表名": {
                "description": None,
                "columns":{
                    "列名":{
                        "value_type": "数据类型",
                        "description": "列描述",
                        "example_values": None
                    }
                }
            }
        }
    """
    schema_ir: dict[str, Any] = {}

    if workspace_path is None or output_path is None:
        return schema_ir

    pattern = os.path.join(output_path, "output_tables_with_columns_*.json")
    json_files = glob.glob(pattern)

    if not json_files:
        return schema_ir

    for file_path in json_files:
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)

        for table_name, columns_list in data.items():
            if table_name not in schema_ir:
                schema_ir[table_name] = {"description": None, "columns": {}}

            for col_info in columns_list:
                column_name = col_info.get("column_name", "")
                if column_name:
                    schema_ir[table_name]["columns"][column_name] = {
                        "value_type": col_info.get("column_type"),
                        "description": col_info.get("column_description"),
                        "example_values": None,
                    }

    # 保存 schema_ir 中间表示
    if save_file and schema_ir:
        save_path = os.path.join(workspace_path, "schema_zhongduanyun_schemair.md")
        with open(save_path, "w", encoding="utf-8") as f:
            f.write("schema_ir = ")
            f.write(json.dumps(schema_ir, ensure_ascii=False, indent=2))

    # 复制到 nl2sql subagent 指定的目标目录
    if save_file and schema_ir and target_dir:
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, "schema_zhongduanyun_schemair.md")
        with open(target_path, "w", encoding="utf-8") as f:
            f.write("schema_ir = ")
            f.write(json.dumps(schema_ir, ensure_ascii=False, indent=2))

    return schema_ir


def _fmt(original: str, frontend: str, data: Any) -> dict:
    """
    格式化 metric_instance 搜索工具返回结果。

    Args:
        original: 原始消息，通常为 JSON 字符串。
        frontend: 前端展示用摘要。
        data: 实际结构化数据。

    Returns:
        dict: 包含原始消息、前端摘要和结构化数据的结果字典。
    """
    return {"original_msg": original, "frontend_msg": frontend, "data": data}
