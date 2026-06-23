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
from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.utils.info_utils import get_current_query
from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.semantic_tool.get_join_relations import get_join_relations
from dataagent.actions.tools.semantic_tool.get_table_desc import get_table_description


# ============================================================
# 工具主函数
# ============================================================
def search_metric_instance(keywords: list[str], *, _tool_context: ToolExecutionContext) -> dict:
    """
    根据关键词搜索元数据指标相关的表列信息。

    该工具先粗召回 metric_instance、data_column 和 data_table，再分别沿三类实体扩充召回结果，
    最后汇总表列信息、保存中间文件并生成 nl2sql 使用的 schema 中间表示。

    Args:
        keywords: 关键词列表，例如 ["用户", "app二级分类", "使用时长", "7天", "14天", "30天"]。

    Returns:
        dict: 包含原始 JSON 字符串、前端摘要和结构化数据的工具返回值。
    """
    # 语义感知增强-元数据增强模块 基础URL和认证
    base_url = _tool_context.config_manager.get("METAVISOR.metavisor_url")
    auth = ("admin", "admin")

    # Step 1: 根据 ”模型传入的 keywords”，粗召回筛选出与 keywords 相关的 “metric_instance、data_column、data_table”
    result_json = _coarse_recall_metric_instances(keywords, base_url, auth)

    # Step 2: 根据 “粗召回的 typeName”，筛选出 “metric_instance、data_column、data_table 的 qualified_name”
    typed_recall_result = _filter_metric_instances_by_type(
        result_json, ("metric_instance", "data_column", "data_table")
    )
    metric_instance_fulltext_list = typed_recall_result["metric_instance"]
    data_column_fulltext_list = typed_recall_result["data_column"]
    data_table_fulltext_list = typed_recall_result["data_table"]
    metric_instance_list = [entity["qualified_name"] for entity in metric_instance_fulltext_list]
    data_column_list = [entity["qualified_name"] for entity in data_column_fulltext_list]
    data_table_list = [entity["qualified_name"] for entity in data_table_fulltext_list]

    # Step 3-8: 根据 Step2 粗召回返回的 “metric_instance 的 qualified_name”，独立扩充召回结果
    metric_instance_recall_result = _recall_from_metric_instances(metric_instance_list, base_url, auth)
    metric_instance_column_list = metric_instance_recall_result["metric_instance_column_list"]
    metric_instance_table_list = metric_instance_recall_result["metric_instance_table_list"]
    tables_from_metric_instance_column_list = metric_instance_recall_result["tables_from_metric_instance_column_list"]
    columns_from_metric_instance_column_tables = metric_instance_recall_result[
        "columns_from_metric_instance_column_tables"
    ]
    columns_from_metric_instance_table_list = metric_instance_recall_result["columns_from_metric_instance_table_list"]
    tables_with_columns = metric_instance_recall_result["tables_with_columns"]

    # Step 9-10: 根据 Step2 粗召回返回的 “data_column qualified_name”，独立扩充召回结果
    data_column_recall_result = _recall_from_data_columns(data_column_list, tables_with_columns, base_url, auth)
    tables_from_data_column_list = data_column_recall_result["tables_from_data_column_list"]
    tables_with_columns = data_column_recall_result["tables_with_columns"]

    # Step 11-12: 根据 Step2 粗召回返回的 “data_table qualified_name”，独立扩充召回结果
    data_table_recall_result = _recall_from_data_tables(data_table_list, tables_with_columns, base_url, auth)
    columns_from_data_table_list = data_table_recall_result["columns_from_data_table_list"]
    tables_with_columns = data_table_recall_result["tables_with_columns"]

    # Step 13: 补充表的描述信息
    described_tables = _attach_table_descriptions_to_metric_results(
        {
            "tables_from_metric_instance_column_list": tables_from_metric_instance_column_list,
            "columns_from_metric_instance_column_tables": columns_from_metric_instance_column_tables,
            "columns_from_metric_instance_table_list": columns_from_metric_instance_table_list,
            "tables_from_data_column_list": tables_from_data_column_list,
            "columns_from_data_table_list": columns_from_data_table_list,
            "tables_with_columns": tables_with_columns,
        },
        base_url,
        auth,
    )
    tables_from_metric_instance_column_list = described_tables["tables_from_metric_instance_column_list"]
    columns_from_metric_instance_column_tables = described_tables["columns_from_metric_instance_column_tables"]
    columns_from_metric_instance_table_list = described_tables["columns_from_metric_instance_table_list"]
    tables_from_data_column_list = described_tables["tables_from_data_column_list"]
    columns_from_data_table_list = described_tables["columns_from_data_table_list"]
    tables_with_columns = described_tables["tables_with_columns"]

    # Step 14: 对扩充召回结果做 LLM 精筛，并整合到最终结果
    query_str = get_current_query(_tool_context.runtime)
    logger.warning(f"==== original_query_str: {query_str}")

    output_metric_final = _build_llm_filtered_output_metric_final(
        query_str,
        tables_from_metric_instance_column_list,
        {
            "columns_from_metric_instance_column_tables": columns_from_metric_instance_column_tables,
            "columns_from_metric_instance_table_list": columns_from_metric_instance_table_list,
            "tables_from_data_column_list": tables_from_data_column_list,
            "columns_from_data_table_list": columns_from_data_table_list,
        },
    )

    # Step 15: 生成 summary 并 保存结果
    summary, workspace_path, output_path = _generate_summary_and_save_results(
        keywords,
        query_str,
        result_json,
        metric_instance_list,
        output_metric_final,
        {
            "1_output_metric_coarse_recall": result_json,
            "2_1_output_fulltext_metric_instance_list": metric_instance_fulltext_list,
            "2_2_output_fulltext_data_column_list": data_column_fulltext_list,
            "2_3_output_fulltext_data_table_list": data_table_fulltext_list,
            "3_1_output_metric_instance_column_list": metric_instance_column_list,
            "3_2_output_metric_instance_table_list": metric_instance_table_list,
            "4_output_tables_from_metric_instance_column_list": tables_from_metric_instance_column_list,
            "5_output_columns_from_metric_instance_column_tables": columns_from_metric_instance_column_tables,
            "6_output_columns_from_metric_instance_table_list": columns_from_metric_instance_table_list,
            "9_output_tables_from_data_column_list": tables_from_data_column_list,
            "11_output_columns_from_data_table_list": columns_from_data_table_list,
            "output_tables_with_columns": tables_with_columns,
            "output_metric_final": output_metric_final,
        },
    )

    # Step 16: 生成 schema 中间表示并保存到当前工作空间
    _build_schema_ir_for_nl2sql(
        workspace_path=str(workspace_path),
        output_path=str(output_path),
        base_url=base_url,
        save_file=True,
        _tool_context=_tool_context,
    )

    # Step 17: 返回结果
    original_msg = json.dumps(
        {
            "output_metric_final": output_metric_final,
        },
        ensure_ascii=False,
        indent=2,
    )

    return _fmt(
        original_msg,
        summary,
        {
            "output_metric_final": output_metric_final,
        },
    )


# ============================================================
# 工具辅助函数
# ============================================================


# ----------------------------
# 高层流程辅助函数（按调用顺序）
# ----------------------------


def _coarse_recall_metric_instances(keywords: list[str], base_url: str, auth: tuple) -> dict:
    """
    Step 1：根据关键词执行全文粗召回。

    粗召回结果包含与关键词相关的 metric_instance、data_column、data_table 等实体，
    后续步骤会基于 typeName 进行分流。

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


def _filter_metric_instances_by_type(
    search_result: dict, target_types: tuple[str, ...] = ("metric_instance", "data_column", "data_table")
) -> dict[str, list[dict[str, str]]]:
    """
    Step 2：从粗召回结果中按实体类型(metric_instance, data_column, data_table)筛选实体摘要。

    data_table 的 qualified_name 会去掉存储后缀（例如 @hive），以便直接调用表列查询接口。
    每个实体摘要会保留 qualified_name 和 description，便于排查粗召回命中内容。

    Args:
        search_result: _coarse_recall_metric_instances 的返回结果
        target_types: 需要筛选的实体类型名称

    Returns:
        dict[str, list[dict[str, str]]]: 按类型分组的实体摘要列表。
    """
    filtered_result: dict[str, list[dict[str, str]]] = {type_name: [] for type_name in target_types}

    for entity in search_result.get("fullTextResult", []):
        entity_info = entity.get("entity", {})
        type_name = entity_info.get("typeName")
        if type_name not in filtered_result:
            continue

        attributes = entity_info.get("attributes", {})
        qualified_name = attributes.get("qualified_name")
        if not qualified_name:
            continue
        if type_name == "data_table":
            qualified_name = qualified_name.split("@")[0]
        if any(item["qualified_name"] == qualified_name for item in filtered_result[type_name]):
            continue

        filtered_result[type_name].append(
            {
                "qualified_name": qualified_name,
                "description": _extract_entity_description(attributes),
            }
        )

    return filtered_result


def _recall_from_metric_instances(metric_instance_list: list[str], base_url: str, auth: tuple) -> dict[str, Any]:
    """
    Step 3-8：根据 metric_instance qualified_name 独立扩充召回结果。

    该链路先从指标实例提取源列和实现表，再分别获取指标列详情、指标列所在表的所有列、
    指标表的所有列，并合并为一个表列召回结果。

    Args:
        metric_instance_list: metric_instance qualified_name 列表
        base_url: API基础URL
        auth: 认证元组

    Returns:
        dict[str, Any]: metric_instance 召回链路的中间结果和合并后的表列信息。
    """
    # Step 3: 根据 “metric_instance 的 qualified_name”，提取出 ”指标列的 qualified_name” 和 “指标表的 qualified_name”
    metric_instance_column_list, metric_instance_table_list = _extract_columns_and_tables_from_metric(
        metric_instance_list, base_url, auth
    )

    # Step 4: 根据 “指标列的 qualified_name”，获取 “指标列详情“ 和 ”指标列所在表信息”
    tables_from_metric_instance_column_list = _get_column_details(metric_instance_column_list, base_url, auth)
    tables_with_columns = copy.deepcopy(tables_from_metric_instance_column_list)

    # Step 5: 根据 “指标列所在表的 qualified_name”，获取 “指标列所在表的所有列信息”（扩充召回结果）
    tables_from_columns = list(tables_from_metric_instance_column_list.keys())
    columns_from_metric_instance_column_tables = _get_table_all_columns_details(tables_from_columns, base_url, auth)

    # Step 6: 根据 “指标表的 qualified_name”，获取 “指标表的所有列信息”（扩充召回结果）
    columns_from_metric_instance_table_list = _get_table_all_columns_details(metric_instance_table_list, base_url, auth)

    # Step 7（可选）: 合并 Step4 和 Step5
    # （可选）tables_with_columns = _merge_table_columns(tables_with_columns, columns_from_metric_instance_column_tables)

    # Step 8（可选）: 合并 Step6 和 Step7
    # （可选）tables_with_columns = _merge_table_columns(tables_with_columns, columns_from_metric_instance_table_list)

    return {
        "metric_instance_column_list": metric_instance_column_list,
        "metric_instance_table_list": metric_instance_table_list,
        "tables_from_metric_instance_column_list": tables_from_metric_instance_column_list,
        "columns_from_metric_instance_column_tables": columns_from_metric_instance_column_tables,
        "columns_from_metric_instance_table_list": columns_from_metric_instance_table_list,
        "tables_with_columns": tables_with_columns,
    }


def _recall_from_data_columns(
    data_column_list: list[str], tables_with_columns: dict, base_url: str, auth: tuple
) -> dict[str, Any]:
    """
    Step 9-10：根据 data_column qualified_name 独立扩充召回结果。

    该链路只补充粗召回命中的 data_column 自身列详情和所在表信息，不再按所在表扩充全表列。

    Args:
        data_column_list: data_column qualified_name 列表
        tables_with_columns: 已有的表列召回结果
        base_url: API基础URL
        auth: 认证元组

    Returns:
        dict[str, Any]: data_column 召回链路的中间结果和合并后的表列信息。
    """
    # Step 9: 根据 Step2 返回的 “data_column qualified_name”，获取列详情和所在表信息（扩充召回结果）
    tables_from_data_column_list = _get_column_details(data_column_list, base_url, auth)

    # Step 10（可选）: 合并 Step2 中 data_column 独立扩充出的召回结果
    tables_with_columns = _merge_table_columns(tables_with_columns, tables_from_data_column_list)

    return {
        "tables_from_data_column_list": tables_from_data_column_list,
        "tables_with_columns": tables_with_columns,
    }


def _recall_from_data_tables(
    data_table_list: list[str], tables_with_columns: dict, base_url: str, auth: tuple
) -> dict[str, Any]:
    """
    Step 11-12：根据 data_table qualified_name 独立扩充召回结果。

    该链路会获取粗召回命中的 data_table 的所有列，并合并到已有表列召回结果中。

    Args:
        data_table_list: data_table qualified_name 列表
        tables_with_columns: 已有的表列召回结果
        base_url: API基础URL
        auth: 认证元组

    Returns:
        dict[str, Any]: data_table 召回链路的中间结果和合并后的表列信息。
    """
    # Step 11: 根据 Step2 返回的 “data_table qualified_name”，获取表的所有列信息（扩充召回结果）
    columns_from_data_table_list = _get_table_all_columns_details(data_table_list, base_url, auth)

    # Step 12（可选）: 合并 Step2 中 data_table 独立扩充出的召回结果
    tables_with_columns = _merge_table_columns(tables_with_columns, columns_from_data_table_list)

    return {
        "columns_from_data_table_list": columns_from_data_table_list,
        "tables_with_columns": tables_with_columns,
    }


def _generate_summary_and_save_results(
    keywords: list[str],
    query_str: str,
    result_json: dict,
    metric_instance_list: list[str],
    tables_with_columns: dict,
    result_files: dict[str, Any],
) -> tuple[str, Any, Any]:
    """
    Step 13：生成 summary 并保存召回结果。

    该函数负责创建 .metric_dir 输出目录，保存各步骤中间 JSON 文件和 summary 文本，
    并把后续生成 schema 所需的工作空间路径、输出目录路径返回给主流程。

    Args:
        keywords: 关键词列表
        query_str: 用户原始查询
        result_json: 粗召回结果
        metric_instance_list: 精筛后的 metric_instance qualified_name 列表
        tables_with_columns: 最终合并后的表列信息
        result_files: 待保存的文件名前缀和数据

    Returns:
        tuple[str, Any, Any]: summary、workspace_path、output_path
    """
    summary = _generate_summary(
        keywords,
        query_str,
        len(result_json.get("fullTextResult", [])),
        len(metric_instance_list),
        tables_with_columns,
    )

    guard = get_current_sandbox()
    workspace_path = guard.workspace_root
    if workspace_path is None:
        raise ValueError("workspace_root is required to save metric search results")

    output_path = workspace_path / ".metric_dir"
    os.makedirs(output_path, exist_ok=True)
    current_time = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    logger.info(f"output_path_for_metric: {output_path}")

    for file_prefix, data in result_files.items():
        _save_json_file(data, os.path.join(output_path, f"{file_prefix}_{current_time}.json"))

    with open(os.path.join(output_path, f"output_metric_summary_{current_time}.txt"), "w", encoding="utf-8") as f:
        f.write(summary)

    return summary, workspace_path, output_path


def _build_schema_ir_for_nl2sql(
    workspace_path: str | None = None,
    output_path: str | None = None,
    base_url: str | None = None,
    save_file: bool = True,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """
    Step 14：汇总最终表列召回结果，生成 nl2sql 使用的 schema 中间表示。

    函数会读取 .metric_dir 中最新一批 output_metric_final_*.json 文件内容，
    转换为 schema_ir 字典，并按需写入当前工作空间。

    Args:
        workspace_path: 工作空间路径。为空时直接返回空 schema_ir。
        output_path: .metric_dir 输出目录。为空时直接返回空 schema_ir。
        save_file: 是否在工作空间下保存 schema_schemair.md。

    Returns:
        dict[str, Any]: 整合后的 schema_ir 字典。
    """
    schema_ir: dict[str, Any] = {}
    table_names: list[str] = []

    if workspace_path is None or output_path is None:
        return schema_ir

    pattern = os.path.join(output_path, "output_metric_final_*.json")
    json_files = glob.glob(pattern)

    if not json_files:
        return schema_ir

    for file_path in json_files:
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)

        for table_name, table_info in data.items():
            if table_name not in schema_ir:
                schema_ir[table_name] = {
                    "description": table_info.get("table_description", ""),
                    "columns": {},
                }
            if table_name not in table_names:
                table_names.append(table_name)

            columns_list = table_info.get("columns", [])
            for col_info in columns_list:
                column_name = col_info.get("column_name", "")
                if column_name:
                    schema_ir[table_name]["columns"][column_name] = {
                        "value_type": col_info.get("column_type"),
                        "description": col_info.get("column_description"),
                        "example_values": None,
                    }

    # 获取表的 join 关系
    join_relations: list[dict[str, Any]] = []
    if table_names:
        join_result = get_join_relations(table_names=table_names, _tool_context=_tool_context)
        join_relations = join_result.get("data", {}).get("joins", [])

    # 保存 schema_ir 和 join_relations 中间表示
    if save_file and schema_ir:
        save_path = os.path.join(workspace_path, "schema_schemair.md")
        with open(save_path, "w", encoding="utf-8") as f:
            f.write("schema_ir = ")
            f.write(json.dumps(schema_ir, ensure_ascii=False, indent=2))
            f.write("\n\n")
            f.write("join_relations = ")
            f.write(json.dumps(join_relations, ensure_ascii=False, indent=2))

    return schema_ir


def _fmt(original: str, frontend: str, data: Any) -> dict:
    """
    Step 15：格式化 search_metric_instance 的工具返回值。

    Args:
        original: 原始消息，通常为 JSON 字符串。
        frontend: 前端展示用摘要。
        data: 实际结构化数据。

    Returns:
        dict: 包含 original_msg、frontend_msg 和 data 的结果字典。
    """
    return {"original_msg": original, "frontend_msg": frontend, "data": data}


# ----------------------------
# 底层通用辅助函数
# ----------------------------


def _extract_entity_description(attributes: dict) -> str:
    """
    从全文检索实体属性中提取描述文本。

    不同实体类型的描述字段名可能不同，因此按常见字段优先级依次读取，
    没有描述时返回空字符串。

    Args:
        attributes: 粗召回实体的 attributes 字段。

    Returns:
        str: 实体描述文本。
    """
    description = (
        attributes.get("description")
        or attributes.get("column_description")
        or attributes.get("column_short_description")
        or attributes.get("table_description")
        or attributes.get("table_short_description")
        or attributes.get("metric_description")
        or ""
    )
    return str(description)


def _extract_columns_and_tables_from_metric(
    metric_qualified_names: list[str], base_url: str, auth: tuple
) -> tuple[list[str], list[str]]:
    """
    Step 3：根据 metric_instance qualified_name 提取指标列和指标表。

    函数会逐个查询 metric_instance 详情，从 relationshipAttributes.sourceColumns
    提取 data_column，从 relationshipAttributes.realizedTables 提取 data_table。

    Args:
        metric_qualified_names: metric_instance qualified_name 列表
        base_url: API基础URL
        auth: 认证元组

    Returns:
        tuple[list[str], list[str]]: 指标列 qualified_name 列表、指标表名列表。
    """
    column_list = []
    table_list = []
    for metric_name in metric_qualified_names:
        metric_detail_url = f"{base_url}/api/metaVisor/v3/entity/uniqueAttribute/type/metric_instance"
        metric_params = {"attr:qualified_name": metric_name}
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
    Step 4/9：根据列 qualified_name 获取列详情和所在表信息。

    返回结果按表名分组，每个列只保留列英文名、列描述和数据类型，供最终 schema 汇总使用。

    Args:
        column_qualified_names: 列 qualified_name 列表
        base_url: API基础URL
        auth: 认证元组

    Returns:
        dict: {表名: [列信息列表]}。
    """
    tables_columns = {}
    for column_qualified_name in column_qualified_names:
        column_detail_url = f"{base_url}/api/metaVisor/v3/entity/uniqueAttribute/type/data_column"
        column_params = {"attr:qualified_name": column_qualified_name}
        column_detail = requests.get(column_detail_url, params=column_params, auth=auth).json()

        table_name = column_detail.get("entity", {}).get("attributes", {}).get("table_id")
        column = column_detail.get("entity", {}).get("attributes", {})
        column_kept = {
            "column_name": column.get("column_name_en", ""),
            "column_description": _normalize_column_description(column.get("column_description", "")),
            "column_type": column.get("value_type", ""),
        }
        if table_name not in tables_columns:
            tables_columns[table_name] = []
        if column_kept not in tables_columns[table_name]:
            tables_columns[table_name].append(column_kept)

    return tables_columns


def _get_table_all_columns_details(table_names: list[str], base_url: str, auth: tuple) -> dict:
    """
    Step 5/6/11：根据表名获取表的所有列信息。

    该函数调用 table-columns-info 接口，将接口返回的列全名转换为最终 schema 使用的短列名。

    Args:
        table_names: 表名列表
        base_url: API基础URL
        auth: 认证元组

    Returns:
        dict: {表名: [列信息列表]}。
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
                "column_description": _normalize_column_description(
                    all_columns[column_name]["column_short_description"]
                ),
                "column_type": all_columns[column_name]["value_type"],
            }
            for column_name in all_columns
        ]

    return tables_columns


def _normalize_column_description(description: Any) -> str:
    """
    规范化列描述文本。

    如果描述中同时包含“列英文名：”、“列中文名：”、“列描述：”，只保留“列描述：”后的内容。

    Args:
        description: 原始列描述

    Returns:
        str: 规范化后的列描述。
    """
    description_text = str(description or "")
    markers = ("列英文名：", "列中文名：", "列描述：")
    if all(marker in description_text for marker in markers):
        return description_text.split("列描述：", 1)[1].strip()
    return description_text


def _attach_table_descriptions_to_metric_results(metric_results: dict[str, dict], base_url: str, auth: tuple) -> dict:
    """
    为 metric 召回链路的各类表列结果统一补充表描述。

    Args:
        metric_results: 多个表列结果字典，value 结构为 {表名: [列信息列表]}
        base_url: API基础URL
        auth: 认证元组

    Returns:
        dict: 已补充表描述的多个表列结果字典。
    """
    table_description_cache: dict[str, str] = {}
    return {
        result_name: _attach_table_descriptions(tables_columns, base_url, auth, table_description_cache)
        for result_name, tables_columns in metric_results.items()
    }


def _build_llm_filtered_output_metric_final(
    query_str: str,
    base_tables: dict[str, dict],
    candidate_results: dict[str, dict],
    top_k: int = 50,
) -> dict[str, dict]:
    """
    使用 LLM 从扩充召回结果中选择与用户查询最相关的 top_k 个表列，并合并到最终结果。

    Args:
        query_str: 用户原始查询
        base_tables: 不参与模型筛选、直接保留的表列结果
        candidate_results: 需要模型筛选的扩充召回结果
        top_k: 最多保留的候选表列数量

    Returns:
        dict[str, dict]: 合并后的 output_metric_final。
    """
    output_metric_final = copy.deepcopy(base_tables)
    candidates = _flatten_metric_column_candidates(candidate_results)
    if not candidates:
        return output_metric_final

    selected_candidates = _select_top_metric_columns_by_llm(query_str, candidates, top_k)
    for candidate in selected_candidates:
        _merge_described_column_into_result(output_metric_final, candidate)

    return output_metric_final


def _flatten_metric_column_candidates(candidate_results: dict[str, dict]) -> list[dict[str, Any]]:
    """
    将多个已补充表描述的表列结果压平成 LLM 可判断的候选表列列表。

    Args:
        candidate_results: 结构为 {结果名: {表名: {table_description, columns}}}

    Returns:
        list[dict[str, Any]]: 去重后的候选表列列表。
    """
    candidates = []
    seen = set()
    for source_name, tables in candidate_results.items():
        for table_name, table_info in tables.items():
            for column in table_info.get("columns", []):
                column_name = column.get("column_name", "")
                if not column_name:
                    continue
                candidate_key = (table_name, column_name)
                if candidate_key in seen:
                    continue
                seen.add(candidate_key)
                candidates.append(
                    {
                        "source": source_name,
                        "table_name": table_name,
                        "table_description": table_info.get("table_description", ""),
                        "column_name": column_name,
                        "column_description": column.get("column_description", ""),
                        "column_type": column.get("column_type", ""),
                    }
                )
    return candidates


def _select_top_metric_columns_by_llm(
    query_str: str,
    candidates: list[dict[str, Any]],
    top_k: int,
) -> list[dict[str, Any]]:
    """
    调用默认 LLM，从候选表列中选择最相关的 top_k 项。

    Args:
        query_str: 用户原始查询
        candidates: 候选表列列表
        top_k: 最多选择数量

    Returns:
        list[dict[str, Any]]: 被选中的候选表列。
    """
    candidate_payload = [
        {
            "id": idx,
            "table_name": candidate["table_name"],
            "table_description": candidate["table_description"],
            "column_name": candidate["column_name"],
            "column_description": candidate["column_description"],
            "column_type": candidate["column_type"],
        }
        for idx, candidate in enumerate(candidates)
    ]
    prompt = _build_metric_column_selection_prompt(query_str, candidate_payload, top_k)

    try:
        llm = llm_manager.get_default_llm()
        response = llm.invoke(
            [
                {
                    "role": "system",
                    "content": "你是元数据检索排序专家，只能输出严格 JSON。",
                },
                {"role": "user", "content": prompt},
            ]
        )
        selected_ids = _parse_selected_candidate_ids(response.content, len(candidates), top_k)
    except Exception as err:
        logger.warning(f"LLM metric column selection failed: {err}")
        selected_ids = []

    return [candidates[idx] for idx in selected_ids]


def _build_metric_column_selection_prompt(
    query_str: str,
    candidate_payload: list[dict[str, Any]],
    top_k: int,
) -> str:
    """
    构造候选表列相关性筛选提示词。

    Args:
        query_str: 用户原始查询
        candidate_payload: 候选表列载荷
        top_k: 最多选择数量

    Returns:
        str: LLM 提示词。
    """
    return (
        "请根据用户原始查询，从候选表列中选择最相关的表和列，最多选择 "
        f"{top_k} 项。\n"
        "选择标准：优先匹配业务含义、表描述、列描述、列名；不要选择弱相关或重复含义的列。\n"
        "只输出 JSON，不要输出 Markdown 或解释文字。格式如下：\n"
        '{"selected_ids": [0, 1, 2]}\n\n'
        f"用户原始查询：{query_str}\n"
        f"候选表列：{json.dumps(candidate_payload, ensure_ascii=False)}"
    )


def _parse_selected_candidate_ids(raw_response: str, candidate_count: int, top_k: int) -> list[int]:
    """
    解析 LLM 返回的 selected_ids，并做范围和数量校验。

    Args:
        raw_response: LLM 原始输出
        candidate_count: 候选项总数
        top_k: 最多选择数量

    Returns:
        list[int]: 有效候选 ID。
    """
    parsed = _loads_json_from_llm_response(raw_response)
    selected_ids = parsed.get("selected_ids", [])
    if not isinstance(selected_ids, list):
        return []

    result = []
    for raw_id in selected_ids:
        if not isinstance(raw_id, int) or raw_id < 0 or raw_id >= candidate_count:
            continue
        if raw_id in result:
            continue
        result.append(raw_id)
        if len(result) >= top_k:
            break
    return result


def _loads_json_from_llm_response(raw_response: str) -> dict:
    """
    从 LLM 输出中提取 JSON 对象。

    Args:
        raw_response: LLM 原始输出

    Returns:
        dict: 解析后的 JSON 对象。
    """
    text = raw_response.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(text[start : end + 1])

    if not isinstance(parsed, dict):
        raise ValueError("LLM selection response must be a JSON object")
    return parsed


def _merge_described_column_into_result(output_metric_final: dict[str, dict], candidate: dict[str, Any]) -> None:
    """
    将一个 LLM 选中的候选表列合并到最终结果。

    Args:
        output_metric_final: 最终表列结果，原地更新
        candidate: LLM 选中的候选表列

    Returns:
        None。
    """
    table_name = candidate["table_name"]
    column = {
        "column_name": candidate["column_name"],
        "column_description": candidate.get("column_description", ""),
        "column_type": candidate.get("column_type", ""),
    }
    if table_name not in output_metric_final:
        output_metric_final[table_name] = {
            "table_name": table_name,
            "table_description": candidate.get("table_description", ""),
            "columns": [column],
        }
        return

    columns = output_metric_final[table_name]["columns"]
    if column not in columns:
        columns.append(column)


def _attach_table_descriptions(
    tables_columns: dict,
    base_url: str,
    auth: tuple,
    description_cache: dict[str, str] | None = None,
) -> dict:
    """
    为表列信息补充表描述，并转换成包含 description 和 columns 的结构。

    Args:
        tables_columns: 表列信息字典，结构为 {表名: [列信息列表]}
        base_url: API基础URL
        auth: 认证元组
        description_cache: 表描述缓存，避免多个中间结果重复查询同一张表

    Returns:
        dict: {表名: {"table_name": 表名, "table_description": 表描述, "columns": [列信息列表]}}
    """
    result = {}
    cache = description_cache if description_cache is not None else {}
    for table_name, columns in tables_columns.items():
        table_qualified_name = f"{table_name}@hive"
        if table_qualified_name not in cache:
            cache[table_qualified_name] = get_table_description(table_qualified_name, base_url, auth)
        result[table_name] = {
            "table_name": table_name,
            "table_description": cache[table_qualified_name],
            "columns": columns,
        }
    return result


def _merge_table_columns(target: dict, source: dict) -> dict:
    """
    Step 7/8/10/12：将 source 中的表列信息合并到 target。

    合并时会保留 target 中已有内容，并避免向同一个表重复追加完全相同的列信息。

    Args:
        target: 目标字典 {表名: [列信息列表]}
        source: 源字典 {表名: [列信息列表]}

    Returns:
        dict: 合并后的表列信息字典。
    """
    for table_name, columns in source.items():
        if table_name not in target:
            target[table_name] = columns
        else:
            existing_column_names = {column.get("column_name") for column in target[table_name]}
            for column in columns:
                column_name = column.get("column_name")
                if column_name in existing_column_names:
                    continue
                target[table_name].append(column)
                existing_column_names.add(column_name)
    return target


def _generate_summary(
    keywords: list[str],
    query_str: str,
    coarse_count: int,
    refine_count: int,
    tables_with_columns: dict,
) -> str:
    """根据召回计数和最终表列信息生成检索摘要。"""
    column_cnt = sum(len(table_info["columns"]) for table_info in tables_with_columns.values())

    summary = (
        f'检索Agent接收到的用户原始查询 "{query_str}",'
        f'基础解析得到的关键词 "{keywords}",'
        f"粗召回候选 metric_instance {coarse_count} 个，"
        f"结构化精筛得到候选 metric_instance {refine_count} 个，"
        f"最终成功获取 {len(tables_with_columns)} 个表，{column_cnt} 个列，其中："
    )

    for table_name, table_info in tables_with_columns.items():
        summary += f"\n- 表 {table_name} 召回了 {len(table_info['columns'])} 个列"

    return summary


def _save_json_file(data: Any, file_path: str) -> None:
    """将召回过程中的中间结果保存为 JSON 文件。"""
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
