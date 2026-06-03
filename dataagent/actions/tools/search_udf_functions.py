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
    根据名称关键字搜索 UDF 函数信息

    Args:
        udf_name_keyword (str | list): 用于过滤的名称关键字，支持：
            - 字符串：单个关键字（例如 "IsEmpty"）
            - 列表：多个关键字（例如 ["是空", "乱码"]）
            - 逗号分隔字符串：多个关键字（例如 "空,乱码"）
        offset (int): 分页偏移量，默认 0
        limit (int): 分页限制，默认 25
        attributes (list): 返回的属性列表，默认包含主要字段
        attribute_name (str): 搜索的属性名，支持的值包括：function_description、prototype、type、category、description、name、all
                            - 如果未提供该参数（使用默认值 "all"），则同时在所有支持的属性（
                            function_description、prototype、type、category、description、name）中使用 OR 关系搜索
                            - 如果提供了该参数（值为具体的属性名），则只在指定的单个属性中搜索

    Returns:
        dict: {
            "original_msg": str,  # JSON格式的原始数据
            "frontend_msg": str,  # 前端展示用摘要信息
            "data": Any           # 解析后的数据
        }
    """
    # 解析关键字列表
    keywords = _parse_keywords(udf_name_keyword)
    if not keywords:
        return _fmt(
            "未提供 UDF 名称关键字。",
            "没有输入 UDF 名称关键字，请提供搜索关键字",
            {"udf_name_keyword": udf_name_keyword, "entities": [], "approximateCount": 0},
        )

    if attributes is None:
        attributes = ["category", "type", "prototype", "args", "function_description", "examples"]

    cm = _tool_context.config_manager
    if cm is None:
        raise RuntimeError("search_udf_function_by_name_keyword requires per-Agent ConfigManager in _tool_context.")

    # 企业语义服务元数据 UDF 基础搜索 API 基础 URL 和认证
    base_url = cm.get("METAVISOR.metavisor_url")
    auth = ("admin", "admin")

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

    # 生成 summary 并 保存结果
    entities = result_json.get("entities", [])
    approximate_count = result_json.get("approximateCount", 0)
    qualified_names = [entity.get("attributes", {}).get("qualifiedName", "") for entity in entities]
    summary = (
        f"根据关键字 {keywords} 搜索 UDF 函数，"
        f"共找到 {approximate_count} 个匹配结果，返回 {len(entities)} 个 UDF 函数实体：{qualified_names}"
    )

    guard = get_current_sandbox()
    workspace_path = guard.workspace_root
    if workspace_path is None:
        raise ValueError("workspace_root is required to save UDF search results")
    output_path = workspace_path / "udf_basic_dir"
    os.makedirs(output_path, exist_ok=True)
    current_time = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    logger.info(f"output_path_for_udf_basic: {output_path}")

    _save_json_file(result_json, os.path.join(output_path, f"output_basic_search_udf_origin_{current_time}.json"))

    abstract_dict = _extract_udf_abstract(result_json)
    _save_json_file(abstract_dict, os.path.join(output_path, f"output_basic_search_udf_abstract_{current_time}.json"))

    with open(
        os.path.join(output_path, f"output_basic_search_udf_summary_{current_time}.txt"), "w", encoding="utf-8"
    ) as f:
        f.write(summary)

    # 生成 UDF 基础信息并保存到 nl2sql subagent 指定的目录
    _build_udf_basic_for_nl2sql(
        workspace_path=str(workspace_path),
        output_path=str(output_path),
        save_file=True,
        target_dir=cm.get("WORKSPACE.target_path"),
    )

    # 返回结果
    original_msg = json.dumps(result_json, ensure_ascii=False, indent=2)

    return _fmt(original_msg, summary, {"entities": entities, "approximateCount": approximate_count})


def search_udf_function_by_dsl(
    attribute_name: str,
    attribute_value: str,
    operator: str = "like",
    *,
    _tool_context: ToolExecutionContext,
) -> dict:
    """
    使用 DSL 语法搜索 UDF 函数信息（对特定属性进行模糊匹配）

    Args:
        attribute_name (str): 要搜索的属性名（例如 "function_description"）
        attribute_value (str): 要匹配的属性值（例如 "判断是否包含乱码"）
        operator (str): 操作符，支持 "like"（模糊匹配），默认 "like"

    Returns:
        dict: {
            "original_msg": str,  # JSON格式的原始数据
            "frontend_msg": str,  # 前端展示用摘要信息
            "data": Any           # 解析后的数据
        }
    """
    # 标准化属性值
    normalized_attr_value = attribute_value.strip() if isinstance(attribute_value, str) else ""
    if not normalized_attr_value:
        return _fmt(
            "未提供搜索属性值。",
            "没有输入搜索属性值，请提供搜索关键字",
            {"attribute_name": attribute_name, "attribute_value": attribute_value, "entities": []},
        )

    cm = _tool_context.config_manager
    if cm is None:
        raise RuntimeError("search_udf_function_by_dsl requires per-Agent ConfigManager in _tool_context.")

    # 企业语义服务元数据 UDF DSL 搜索 API 基础 URL 和认证
    base_url = cm.get("METAVISOR.metavisor_url")
    auth = ("admin", "admin")

    # 使用 DSL 搜索获取 UDF 函数的 guid
    dsl_result = _dsl_search(
        attribute_name=attribute_name,
        attribute_value=normalized_attr_value,
        operator=operator,
        base_url=base_url,
        auth=auth,
    )

    # 根据 guid 查询 UDF 函数的完整实体信息
    entities = dsl_result.get("entities", [])
    enriched_entities = []

    for entity in entities:
        guid = entity.get("guid")
        if guid:
            entity_detail = _get_entity_by_guid(guid, base_url, auth)
            if entity_detail:
                enriched_entities.append(entity_detail)

    # 生成 summary 并 保存结果
    qualified_names = [entity.get("attributes", {}).get("qualifiedName", "") for entity in enriched_entities]
    summary = (
        f'使用 DSL 搜索属性 "{attribute_name}" 包含 "{normalized_attr_value}"，'
        f"共找到 {len(enriched_entities)} 个 UDF 函数实体：{qualified_names}"
    )

    guard = get_current_sandbox()
    workspace_path = guard.workspace_root
    if workspace_path is None:
        raise ValueError("workspace_root is required to save UDF search results")
    output_path = workspace_path / "udf_dsl_dir"
    os.makedirs(output_path, exist_ok=True)
    current_time = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    logger.info(f"output_path_for_udf_dsl: {output_path}")

    _save_json_file(dsl_result, os.path.join(output_path, f"output_dsl_search_udf_origin_{current_time}.json"))

    abstract_dict = {}
    for entity in enriched_entities:
        attributes = entity.get("attributes", {})
        qualified_name = attributes.get("qualifiedName")
        if qualified_name:
            abstract_dict[qualified_name] = {
                "args": attributes.get("args", []),
                "examples": attributes.get("examples", []),
                "prototype": attributes.get("prototype", ""),
                "function_description": attributes.get("function_description", ""),
            }
    _save_json_file(abstract_dict, os.path.join(output_path, f"output_dsl_search_udf_abstract_{current_time}.json"))

    with open(
        os.path.join(output_path, f"output_dsl_search_udf_summary_{current_time}.txt"), "w", encoding="utf-8"
    ) as f:
        f.write(summary)

    # 返回结果
    original_msg = json.dumps(enriched_entities, ensure_ascii=False, indent=2)

    return _fmt(original_msg, summary, {"entities": enriched_entities, "approximateCount": len(enriched_entities)})


# ============================================================
# 工具辅助函数
# ============================================================


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


def _save_json_file(data: Any, file_path: str) -> None:
    """
    保存 UDF 函数搜索结果 JSON 数据到文件

    Args:
        data: 要保存的数据
        file_path: 文件路径
    """
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _extract_udf_abstract(result_json: dict) -> dict:
    """
    从搜索结果中提取 UDF 函数摘要信息

    Args:
        result_json: API返回的完整JSON结果

    Returns:
        dict: 以 qualifiedName 为 key，提取的字段为 value 的字典
    """
    entities = result_json.get("entities", [])
    abstract_dict = {}

    for entity in entities:
        attributes = entity.get("attributes", {})
        qualified_name = attributes.get("qualifiedName")

        if qualified_name:
            abstract_dict[qualified_name] = {
                "args": attributes.get("args", []),
                "examples": attributes.get("examples", []),
                "prototype": attributes.get("prototype", ""),
                "function_description": attributes.get("function_description", ""),
            }

    return abstract_dict


def _build_udf_basic_for_nl2sql(
    workspace_path: str | None = None,
    output_path: str | None = None,
    save_file: bool = True,
    target_dir: str | None = None,
) -> dict[str, Any]:
    """
    汇总 udf_basic_dir 结果目录中的表列信息，生成 UDF 基础信息并保存到 nl2sql subagent 指定的目录。

    Args:
        workspace_path: 工作空间路径，默认为 None
        output_path: 输出路径，默认为 None
        save_file: 是否保存结果到 udf_zdy.md，默认为 True
        target_dir: nl2sql subagent 目标目录，来自 ``WORKSPACE.target_path``。

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

    pattern = os.path.join(output_path, "output_basic_search_udf_abstract_*.json")
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
                for arg in udf_info.get("args", [])
            ]

            udf_basic[udf_name] = {
                "prototype": udf_info.get("prototype"),
                "description": udf_info.get("function_descripfion"),
                "args": args_list,
                "examples": udf_info.get("examples", []),
            }

    # 保存 UDF 基础信息到文件
    if save_file and udf_basic:
        save_path = os.path.join(workspace_path, "udf_zdy.md")
        with open(save_path, "w", encoding="utf-8") as f:
            f.write("udf_basic = ")
            f.write(json.dumps(udf_basic, ensure_ascii=False, indent=2))

        # 复制到 nl2sql subagent 指定的 目标目录
        if target_dir:
            os.makedirs(target_dir, exist_ok=True)
            target_path = os.path.join(target_dir, "udf_zdy.md")
            with open(target_path, "w", encoding="utf-8") as f:
                f.write("udf_basic = ")
                f.write(json.dumps(udf_basic, ensure_ascii=False, indent=2))

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
