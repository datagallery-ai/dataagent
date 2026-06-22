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

import glob
import json
import os
from datetime import UTC, datetime
from typing import Any

import requests
from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox
from dataagent.actions.tools.semantic_tool.auth import get_metavisor_auth

# ============================================================
# 工具主函数
# ============================================================


def search_udf_function_by_name_keyword(
    udf_name_keyword: str | list | None = None,
    offset: int = 0,
    limit: int = 25,
    attributes: list | None = None,
    attribute_name: str = "all",
    *,
    _tool_context: ToolExecutionContext,
) -> dict:
    """
    根据名称关键字搜索 UDF 函数信息。

    该工具会通过 basic search 按名称、描述、原型等字段召回 UDF 函数，保存原始结果和摘要结果，
    并生成当前工作空间内的 UDF 基础信息文件。

    Args:
        udf_name_keyword: 用于过滤的名称关键字，支持：
            - 字符串：单个关键字（例如 "IsEmpty"）
            - 列表：多个关键字（例如 ["是空", "乱码"]）
            - 逗号分隔字符串：多个关键字（例如 "空,乱码"）
        offset: 分页偏移量，默认 0。
        limit: 分页限制，默认 25。
        attributes: 返回的属性列表，默认包含主要字段。
        attribute_name: 搜索的属性名。传入 "all" 时会在所有支持字段中使用 OR 关系搜索。

    Returns:
        dict: 包含原始 JSON 字符串、前端摘要和结构化数据的工具返回值。
    """
    # 解析关键字列表
    keywords = _parse_keywords(udf_name_keyword)
    if not keywords:
        return _fmt(
            "未提供 UDF 名称关键字。",
            "没有输入 UDF 名称关键字，请提供搜索关键字",
            {"udf_name_keyword": udf_name_keyword, "entities": []},
        )

    if attributes is None:
        attributes = ["category", "type", "prototype", "args", "function_description", "examples", "qualified_name"]

    # 企业语义服务元数据 UDF 基础搜索 API 基础 URL 和认证
    base_url = _tool_context.config_manager.get("METAVISOR.metavisor_url")
    auth = get_metavisor_auth(_tool_context.config_manager)

    # 调用企业语义服务元数据 UDF 基础搜索 API
    supported_attributes = ["function_description", "prototype", "type", "category", "description", "name"]
    search_attributes = supported_attributes if attribute_name == "all" else attribute_name

    result_json = _basic_search_udf_function(
        keywords=keywords,
        offset=offset,
        limit=limit,
        attributes=attributes,
        base_url=base_url,
        auth=auth,
        attribute_name=search_attributes,
    )

    entities = result_json.get("entities", [])

    # 生成 summary 并保存结果
    summary, workspace_path, output_path = _save_basic_search_results(keywords, result_json)

    # 生成 UDF 基础信息并保存到当前工作空间
    _build_udf_for_nl2sql(
        workspace_path=str(workspace_path),
        output_path=str(output_path),
        save_file_name="schema_udf_basic.md",
        save_file=True,
    )

    # 返回结果
    original_msg = json.dumps(
        {
            "result": result_json,
        },
        ensure_ascii=False,
        indent=2,
    )

    return _fmt(
        original_msg,
        summary,
        {
            "entities": entities,
        },
    )


def search_udf_function_by_dsl(
    attribute_name: str,
    attribute_value: str,
    operator: str = "like",
    *,
    _tool_context: ToolExecutionContext,
) -> dict:
    """
    使用 DSL 语法搜索 UDF 函数信息。

    该工具先通过 DSL 搜索获取候选 UDF 的 GUID，再按 GUID 查询完整实体详情，
    最后保存原始 DSL 结果、摘要结果和前端展示摘要。

    Args:
        attribute_name: 要搜索的属性名（例如 "function_description"）。
        attribute_value: 要匹配的属性值（例如 "判断是否包含乱码"）。
        operator: 操作符，支持 "like"（模糊匹配），默认 "like"。

    Returns:
        dict: 包含原始 JSON 字符串、前端摘要和结构化数据的工具返回值。
    """
    # 标准化属性值
    normalized_attr_value = attribute_value.strip() if isinstance(attribute_value, str) else ""
    if not normalized_attr_value:
        return _fmt(
            "未提供搜索属性值。",
            "没有输入搜索属性值，请提供搜索关键字",
            {"attribute_name": attribute_name, "attribute_value": attribute_value, "entities": []},
        )

    # 企业语义服务元数据 UDF DSL 搜索 API 基础 URL 和认证
    base_url = _tool_context.config_manager.get("METAVISOR.metavisor_url")
    auth = get_metavisor_auth(_tool_context.config_manager)

    # 使用 DSL 搜索获取 UDF 函数的 guid
    dsl_result = _dsl_search(
        attribute_name=attribute_name,
        attribute_value=normalized_attr_value,
        operator=operator,
        base_url=base_url,
        auth=auth,
    )

    # 根据 guid 查询 UDF 函数的完整实体信息
    enriched_entities = _enrich_udf_entities_by_guid(dsl_result.get("entities", []), base_url, auth)

    # 生成 summary 并保存结果
    summary, workspace_path, output_path = _save_dsl_search_results(
        attribute_name, normalized_attr_value, dsl_result, enriched_entities
    )

    # 生成 UDF DSL 信息并保存到当前工作空间
    _build_udf_for_nl2sql(
        workspace_path=str(workspace_path),
        output_path=str(output_path),
        final_file_pattern="output_dsl_search_udf_final_*.json",
        save_file_name="schema_udf_dsl.md",
        save_file=True,
    )

    # 返回结果
    original_msg = json.dumps(
        {
            "entities": enriched_entities,
        },
        ensure_ascii=False,
        indent=2,
    )

    return _fmt(
        original_msg,
        summary,
        {
            "entities": enriched_entities,
        },
    )


# ============================================================
# 工具辅助函数
# ============================================================


# ----------------------------
# 高层流程辅助函数
# ----------------------------


def _parse_keywords(udf_name_keyword: str | list | None) -> list:
    """
    解析关键字输入为列表

    Args:
        udf_name_keyword: 关键字输入，支持字符串（单关键字或逗号分隔）、列表

    Returns:
        list: 标准化后的关键字列表
    """
    if not udf_name_keyword:
        return []

    if isinstance(udf_name_keyword, list):
        # 列表输入
        keywords = [kw.strip() for kw in udf_name_keyword if isinstance(kw, str) and kw.strip()]
    elif isinstance(udf_name_keyword, str):
        if "," in udf_name_keyword:
            # 逗号分隔的字符串
            keywords = [kw.strip() for kw in udf_name_keyword.split(",") if kw.strip()]
        else:
            # 单个字符串
            kw = udf_name_keyword.strip()
            keywords = [kw] if kw else []
    else:
        keywords = []

    return keywords


def _save_basic_search_results(keywords: list, result_json: dict) -> tuple[str, Any, Any]:
    """
    保存 UDF basic search 的原始结果、摘要结果和前端摘要文本。

    Args:
        keywords: 标准化后的搜索关键字列表。
        result_json: basic search API 返回的完整 JSON。

    Returns:
        tuple[str, Any, Any]: 前端摘要、工作空间路径、输出目录路径。
    """
    entities = result_json.get("entities", [])
    summary = _generate_basic_search_summary(keywords, entities)

    workspace_path, output_path, current_time = _prepare_udf_output_dir(".udf_basic_dir", "udf_basic")

    _save_json_file(result_json, os.path.join(output_path, f"output_basic_search_udf_origin_{current_time}.json"))
    abstract_dict = _extract_udf_abstract_from_entities(entities)
    _save_json_file(abstract_dict, os.path.join(output_path, f"output_basic_search_udf_final_{current_time}.json"))

    summary_path = os.path.join(output_path, f"output_basic_search_udf_summary_{current_time}.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary)

    return summary, workspace_path, output_path


def _enrich_udf_entities_by_guid(entities: list, base_url: str, auth: tuple) -> list:
    """
    根据 DSL 搜索返回的 GUID 列表查询完整 UDF 实体详情。

    Args:
        entities: DSL 搜索返回的实体摘要列表。
        base_url: API基础URL。
        auth: 认证元组 (用户名, 密码)。

    Returns:
        list: 成功查询到的完整 UDF 实体列表。
    """
    enriched_entities = []

    for entity in entities:
        guid = entity.get("guid")
        if guid:
            entity_detail = _get_entity_by_guid(guid, base_url, auth)
            if entity_detail:
                enriched_entities.append(entity_detail)

    return enriched_entities


def _save_dsl_search_results(
    attribute_name: str, attribute_value: str, dsl_result: dict, enriched_entities: list
) -> tuple[str, Any, Any]:
    """
    保存 UDF DSL search 的原始结果、摘要结果和前端摘要文本。

    Args:
        attribute_name: DSL 搜索属性名。
        attribute_value: 归一化后的 DSL 搜索属性值。
        dsl_result: DSL search API 返回的原始 JSON。
        enriched_entities: 按 GUID 查询到的完整 UDF 实体列表。

    Returns:
        tuple[str, Any, Any]: 前端摘要、工作空间路径、输出目录路径。
    """
    summary = _generate_dsl_search_summary(attribute_name, attribute_value, enriched_entities)
    workspace_path, output_path, current_time = _prepare_udf_output_dir(".udf_dsl_dir", "udf_dsl")

    _save_json_file(dsl_result, os.path.join(output_path, f"output_dsl_search_udf_origin_{current_time}.json"))
    abstract_dict = _extract_udf_abstract_from_entities(enriched_entities)
    _save_json_file(abstract_dict, os.path.join(output_path, f"output_dsl_search_udf_final_{current_time}.json"))

    summary_path = os.path.join(output_path, f"output_dsl_search_udf_summary_{current_time}.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary)

    return summary, workspace_path, output_path


def _fmt(original: str, frontend: str, data: Any) -> dict:
    """
    格式化 UDF 函数搜索工具返回结果。

    Args:
        original: 原始消息（JSON字符串）
        frontend: 前端展示用摘要
        data: 实际数据

    Returns:
        dict: 格式化的结果字典
    """
    return {"original_msg": original, "frontend_msg": frontend, "data": data}


# ----------------------------
# 底层通用辅助函数
# ----------------------------


def _basic_search_udf_function(
    keywords: list,
    offset: int,
    limit: int,
    attributes: list,
    base_url: str,
    auth: tuple,
    attribute_name: str | list = "all",
) -> dict:
    """
    调用 POST /api/metaVisor/v3/search/basic API 进行 UDF 函数基础搜索

    Args:
        keywords: 用于过滤的关键字列表
        offset: 分页偏移
        limit: 分页限制
        attributes: 返回属性列表
        base_url: API基础URL
        auth: 认证元组 (用户名, 密码)
        attribute_name: 搜索的属性名，支持字符串或列表，与 search_udf_function_by_name_keyword 保持一致
                      - 字符串 "all"：在所有支持的属性中使用 OR 关系搜索
                      - 字符串（具体属性名）：在单个指定属性中搜索
                      - 列表：在多个属性中使用 OR 关系搜索

    Returns:
        dict: API返回的JSON结果
    """
    search_url = f"{base_url}/api/metaVisor/v3/search/basic"

    # 统一处理为列表格式
    supported_attributes = ["function_description", "prototype", "type", "category", "description", "name"]

    if isinstance(attribute_name, str):
        attribute_names = supported_attributes if attribute_name == "all" else [attribute_name]
    else:
        attribute_names = attribute_name

    # 构建 entityFilters
    if len(keywords) == 1:
        # 单关键字搜索：在所有属性中使用 OR 关系搜索
        criterion = [
            {"attributeName": attr_name, "operator": "CONTAINS", "attributeValue": keywords[0]}
            for attr_name in attribute_names
        ]
        entity_filters = {"condition": "OR", "criterion": criterion}
    else:
        # 多关键字搜索：构建 condition + criterion 结构，所有关键字和属性都用 OR 连接
        criterion = [
            {"attributeName": attr_name, "operator": "CONTAINS", "attributeValue": kw}
            for attr_name in attribute_names
            for kw in keywords
        ]
        entity_filters = {"condition": "OR", "criterion": criterion}

    payload = {
        "typeName": "udf_function",
        "offset": offset,
        "limit": limit,
        "entityFilters": entity_filters,
        "attributes": attributes,
    }

    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    response = requests.post(search_url, json=payload, headers=headers, auth=auth)
    return response.json()


def _prepare_udf_output_dir(dir_name: str, log_name: str) -> tuple[Any, Any, str]:
    """
    创建 UDF 搜索输出目录并生成本次保存使用的时间戳。

    Args:
        dir_name: 工作空间下的输出目录名称。
        log_name: 日志中展示的输出目录标识。

    Returns:
        tuple[Any, Any, str]: 工作空间路径、输出目录路径、当前时间戳。
    """
    guard = get_current_sandbox()
    workspace_path = guard.workspace_root
    if workspace_path is None:
        raise ValueError("workspace_root is required to save UDF search results")

    output_path = workspace_path / dir_name
    os.makedirs(output_path, exist_ok=True)
    current_time = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    logger.info(f"output_path_for_{log_name}: {output_path}")

    return workspace_path, output_path, current_time


def _generate_basic_search_summary(keywords: list, entities: list) -> str:
    """
    生成 UDF basic search 的前端摘要文本。

    Args:
        keywords: 标准化后的搜索关键字列表。
        entities: basic search 返回的实体列表。

    Returns:
        str: 前端摘要文本。
    """
    qualified_names = [entity.get("attributes", {}).get("qualified_name", "") for entity in entities]
    return f"根据关键字 {keywords} 搜索 UDF 函数，共返回 {len(entities)} 个 UDF 函数实体：{qualified_names}"


def _generate_dsl_search_summary(attribute_name: str, attribute_value: str, enriched_entities: list) -> str:
    """
    生成 UDF DSL search 的前端摘要文本。

    Args:
        attribute_name: DSL 搜索属性名。
        attribute_value: 归一化后的 DSL 搜索属性值。
        enriched_entities: 按 GUID 查询到的完整 UDF 实体列表。

    Returns:
        str: 前端摘要文本。
    """
    qualified_names = [entity.get("attributes", {}).get("qualified_name", "") for entity in enriched_entities]
    return (
        f'使用 DSL 搜索属性 "{attribute_name}" 包含 "{attribute_value}"，'
        f"共找到 {len(enriched_entities)} 个 UDF 函数实体：{qualified_names}"
    )


def _extract_udf_abstract_from_entities(entities: list) -> dict:
    """
    从 UDF 实体列表中提取生成 schema 所需的摘要信息。

    Args:
        entities: UDF 完整实体列表或 basic search 返回的实体列表。

    Returns:
        dict: 以 qualified_name 为 key，提取的字段为 value 的字典。
    """
    abstract_dict = {}

    for entity in entities:
        attributes = entity.get("attributes", {})
        qualified_name = attributes.get("qualified_name")

        if qualified_name:
            abstract_dict[qualified_name] = {
                "args": attributes.get("args", []),
                "examples": attributes.get("examples", []),
                "prototype": attributes.get("prototype", ""),
                "function_description": attributes.get("function_description", ""),
            }

    return abstract_dict


def _save_json_file(data: Any, file_path: str) -> None:
    """
    保存 UDF 函数搜索结果 JSON 数据到文件。

    Args:
        data: 要保存的数据。
        file_path: 文件路径。

    Returns:
        None。
    """
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _build_udf_for_nl2sql(
    workspace_path: str | None = None,
    output_path: str | None = None,
    final_file_pattern: str = "output_basic_search_udf_final_*.json",
    save_file_name: str | None = None,
    save_file: bool = True,
) -> dict[str, Any]:
    """
    汇总 UDF final 结果文件，生成 UDF 基础信息并保存到当前工作空间。

    Args:
        workspace_path: 工作空间路径，默认为 None
        output_path: 输出路径，默认为 None
        final_file_pattern: final 结果文件匹配模式，支持 basic 和 DSL 两类搜索输出
        save_file_name: 保存到工作空间下的 UDF schema 文件名。为 None 时不保存文件。
        save_file: 是否保存结果文件，默认为 True

    Returns:
        整合后的 udf_basic 字典，格式如下:
        {
            "UDF名称": {
                "prototype": "函数原型",
                "description": "函数描述",
                "args": [
                    {
                        "name": "参数名",
                        "type": "参数类型",
                        "description": "参数描述"
                    }
                ],
                "examples": [
                    {
                        "call": "调用示例",
                        "result": "结果"
                    }
                ]
            }
        }
    """
    udf_basic: dict[str, Any] = {}
    if workspace_path is None or output_path is None:
        return udf_basic

    pattern = os.path.join(output_path, final_file_pattern)
    json_files = glob.glob(pattern)

    if not json_files:
        return udf_basic

    for file_path in json_files:
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)

        for udf_name, udf_info in data.items():
            args_list = [
                {
                    "name": arg.get("arg_name"),
                    "type": arg.get("arg_type"),
                    "description": arg.get("arg_description"),
                }
                for arg in json.loads(udf_info.get("args", []).get("value", {}))
            ]

            udf_basic[udf_name] = {
                "prototype": udf_info.get("prototype"),
                "description": udf_info.get("function_description"),
                "args": args_list,
                "examples": udf_info.get("examples", []).get("value", {}),
            }

    # 保存 UDF 基础信息到文件
    if save_file and save_file_name is not None and udf_basic:
        save_path = os.path.join(workspace_path, save_file_name)
        with open(save_path, "w", encoding="utf-8") as f:
            f.write("udf_basic = ")
            f.write(json.dumps(udf_basic, ensure_ascii=False, indent=2))

        logger.warning(f"schema_udf_xxx save succeed: {save_path}")

    return udf_basic


def _dsl_search(attribute_name: str, attribute_value: str, operator: str, base_url: str, auth: tuple) -> dict:
    """
    调用 GET /api/metaVisor/v3/search/dsl API 进行 DSL 搜索

    Args:
        attribute_name: 要搜索的属性名
        attribute_value: 要匹配的属性值
        operator: 操作符（如 "like"）
        base_url: API基础URL
        auth: 认证元组 (用户名, 密码)

    Returns:
        dict: API返回的JSON结果
    """
    # 对特殊字符进行转义处理，与 search_udf_functions 保持一致
    safe_attribute_value = attribute_value.replace("\\", "\\\\").replace('"', '\\"')
    dsl_query = f'udf_function where {attribute_name} {operator} "*{safe_attribute_value}*"'

    search_url = f"{base_url}/api/metaVisor/v3/search/dsl"

    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    # 使用 params 参数，让 requests 自动处理 URL 编码
    response = requests.get(search_url, headers=headers, auth=auth, params={"query": dsl_query})
    return response.json()


def _get_entity_by_guid(guid: str, base_url: str, auth: tuple) -> dict:
    """
    调用 GET /api/metaVisor/v3/entity/guid/{guid} API 获取 UDF 函数的实体详情

    Args:
        guid: 实体的 GUID
        base_url: API基础URL
        auth: 认证元组 (用户名, 密码)

    Returns:
        dict: 实体详情（entity 字段内容），如果失败返回空字典
    """
    entity_url = f"{base_url}/api/metaVisor/v3/entity/guid/{guid}"

    headers = {"Accept": "application/json"}

    response = requests.get(entity_url, headers=headers, auth=auth, verify=False)
    result = response.json()
    return result.get("entity", {})
