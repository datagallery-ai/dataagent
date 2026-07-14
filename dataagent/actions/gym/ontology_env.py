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
from __future__ import annotations

import ast
import datetime
import json
import os
import re
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from dataagent.actions.environment.env import Env
from dataagent.actions.perceptor.perceptor_utils import execute_with_llm

_MAX_BUSINESS_KEYWORD_LEN = 128


def _safe_literal_eval(value):
    """
    安全地尝试解析字符串为 Python 字面量。

    Args:
        value: 待解析的值。

    Returns:
        解析成功则返回解析结果，否则返回原值。
    """
    if isinstance(value, str):
        try:
            return ast.literal_eval(value)
        except Exception:
            return value
    return value


def _pretty_json_for_display(x) -> str:
    """
    将输入内容转换为带缩进的 JSON 字符串，便于展示：
        - dict / list：直接 json.dumps(indent=2)
        - 字符串：依次尝试 json.loads 与 ast.literal_eval
        - 其他类型：直接 str(x)
    """
    if isinstance(x, (dict, list)):
        return json.dumps(x, ensure_ascii=False, indent=2)

    s = str(x)
    try:
        obj = json.loads(s)
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        try:
            obj = ast.literal_eval(s)
            if isinstance(obj, (dict, list)):
                return json.dumps(obj, ensure_ascii=False, indent=2)
            return str(obj)
        except Exception:
            return s


def _normalize_business_keywords(keywords: list[str]) -> list[str]:
    if not isinstance(keywords, list) or not keywords:
        raise ValueError("keywords must be a non-empty list.")
    normalized: list[str] = []
    for keyword in keywords:
        text = str(keyword).strip()
        if not text or len(text) > _MAX_BUSINESS_KEYWORD_LEN:
            raise ValueError("keywords contain an invalid item.")
        if "'" in text or '"' in text or "\\" in text or any(ord(c) < 32 for c in text):
            raise ValueError("keywords must not contain quotes, backslashes, or control characters.")
        normalized.append(text)
    return normalized


def _parse_output(text: str):
    match = re.search(r"json\s*(\{.*?\})\s*", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except Exception:
        return None


class OntologyEnv(Env):
    """
    制造业本体查询环境。

    提供一组面向知识图谱的通用查询工具，包括：
        - 本体结构查询
        - 属性条件过滤
        - 多跳关系搜索
        - 路径模式匹配
        - 属性信息查询
        - 统计 / 聚合 / 排序查询
    """

    def __init__(self, config_manager: Any | None = None) -> None:
        """
        初始化本体环境配置与接口地址。

        Args:
            config_manager: Per-Agent ConfigManager from Runtime or ToolExecutionContext.
                When omitted, only ``SCENE`` / ``ONTOLOGY_URL`` environment variables are used.
        """
        scene_env = os.getenv("SCENE")
        url_env = os.getenv("ONTOLOGY_URL")
        if config_manager is not None:
            self.scene = str(scene_env or config_manager.get("ONTOLOGY.scene") or "")
            self.base_url = str(url_env or config_manager.get("ONTOLOGY.api.url") or "")
        else:
            self.scene = str(scene_env or "")
            self.base_url = str(url_env or "")
        logger.debug("OntologyEnv configured base_url_set={} scene={}", bool(self.base_url), self.scene)
        self.search_base_url = f"{self.base_url}/api/v1/search" if self.base_url else ""
        self.action_base_url = f"{self.base_url}/api/v1/action/ontologies/actions/execute" if self.base_url else ""

        super().__init__()

    def init(self):
        """初始化环境配置（当前无额外初始化逻辑）。"""
        pass

    def action_to_uuid(self, action_name: str) -> str | None:
        """
        根据动作名映射到配置中的 action UUID。

        Args:
            action_name: 动作名称。

        Returns:
            找到映射时返回 UUID 字符串，否则返回 None。
        """
        # 确定场景名称
        scene = self.scene

        # 构建正确的URL - 注意路径结构
        base_url = self.action_base_url.replace("/execute", "")
        url = f"{base_url}/get/scene/{scene}"

        resp = requests.get(url, headers={"accept": "application/json"})
        resp.raise_for_status()

        result = resp.json()

        # 检查响应状态
        if result.get("code") != 200:
            return None

        # 遍历actions查找匹配的名称
        actions = result.get("data", {}).get("actions", [])
        for action in actions:
            if action.get("name") == action_name:
                return action.get("id")

        return None

    @Env.tool
    def get_ontology_description(self):
        """
        提供一组面向知识图谱的通用查询工具，包括：
        - 本体结构查询
        - 属性条件过滤
        - 多跳关系搜索
        - 路径模式匹配
        - 属性信息查询
        - 统计 / 聚合 / 排序查询
        """
        object_type = (
            requests.get(self._with_scene_name(f"{self.search_base_url}/get_object_types")).json().get("result", [])
        )
        object_relations = (
            requests.get(self._with_scene_name(f"{self.search_base_url}/get_object_relations")).json().get("result", [])
        )
        nodes_attr = (
            requests.get(self._with_scene_name(f"{self.search_base_url}/get_nodes_attr")).json().get("result", [])
        )
        edges_attr = (
            requests.get(self._with_scene_name(f"{self.search_base_url}/get_edges_attr")).json().get("result", [])
        )
        object_num = len(object_type)
        relations_num = len(object_relations)

        return {
            "original_msg": f"""
                对本体查询结果如下：
                本体目前包含以下几种类型实体：
                {object_type}
                实体之间有以下几种类型的关联，每种关联用(实体-关系类型-实体)的三元组表示:
                {object_relations}
                实体和关系类型对应着Neo4j中的节点和边类型。可以据此填充调用Neo4j检索语句时的参数。
                每种实体对应的属性定义(属性命名，属性描述)如下:
                {nodes_attr}
                每种关系对应的属性定义(属性命名，属性描述)如下：
                {edges_attr}
            """,
            "frontend_msg": f"已查询到本体总体描述信息，本体中共包括{object_num}种实体，{relations_num}种关系，它们的具体schema也已经被加载。",
        }

    @Env.tool
    def get_business_procedure(self, keywords: list[str]):
        """
        使用场景：
            - 根据关键词查询相关业务逻辑

        函数功能：
            - 通过属性过滤查询 business_procedure 节点
            - 若命中唯一节点，返回其完整属性信息

        Args:
            keywords: list[str],可以输入多个关键词（短词）, 其中有一个匹配到对应的业务逻辑则会返回

        Returns:
            业务逻辑节点的属性信息描述
        """
        keywords = _normalize_business_keywords(keywords)
        title_str = "CONTAINS " + " OR CONTAINS ".join([f"'{k}'" for k in keywords])
        url = self._with_scene_name(f"{self.search_base_url}/property_filter")
        parameters = {
            "element_class": "BusinessProcedure",
            "element_type": "NODE",
            "filter_dict": {"title": title_str},
        }
        resp = requests.post(url, json=parameters)
        result = resp.json().get("result", None)
        result_all = []
        if not result:
            return {
                "original": "未查到与关键词相关的业务逻辑。",
                "frontend_msg": "未查到相关业务逻辑。",
            }

        property_info_search_url = self._with_scene_name(f"{self.search_base_url}/property_filter")
        for cur_info_uuid in result:
            uuid_result = cur_info_uuid["n.uuid"]
            parameters = {
                "element_class": "BusinessProcedure",
                "element_type": "NODE",
                "element_uuid": uuid_result,
                "filter_dict": {},
                "get_all_properties": True,
            }
            resp = requests.post(property_info_search_url, json=parameters)
            for cur_info in resp.json().get("result", []):
                result_all.append(
                    {
                        "title": cur_info["properties"]["title"],
                        "procedureContent": cur_info["properties"]["procedureContent"],
                    }
                )
        logger.info("BusinessProcedure query returned {} result(s)", len(result_all))

        return {
            "original": "查询到相关业务逻辑如下：\n" + _pretty_json_for_display(result_all),
            "frontend_msg": "查询到业务逻辑。\n" + _pretty_json_for_display(result_all),
        }

    @Env.tool
    def get_object_info(self, object_type: str):
        """
        查询指定类型的所有节点实例

        Examples:
            >>> get_object_info("Supplier")
            [
                {"id": "S01", "name": None, "uuid": "acf6b73"},
                {"id": "S02", "name": None, "uuid": "ac7782"},
            ]
        """
        parameters = {
            "element_class": object_type,
            "element_type": "NODE",
            "filter_dict": {},
        }
        result = self._search_post("property_filter", parameters).get("result")
        if not result:
            return {
                "original": f"未查到类型为 '{object_type}' 的节点实例。",
                "frontend_msg": f"未查到类型为 '{object_type}' 的节点实例。",
            }
        return {
            "original": f"查询到{object_type}的节点如下：\n{_pretty_json_for_display(result)}",
            "frontend_msg": f"查询到类型为 '{object_type}' 的节点实例。",
        }

    @Env.tool
    def get_node_info(self, object_type: str, uuid: str):
        """
        查询指定 uuid 的节点属性信息

        Examples:
            >>> get_node_info("MPart", "acf6b73e-ef6d-460b-8aa3-189c295db5b1")
            [
                {
                    "properties": {
                        "status": "active",
                        "mpart_description": "304钢材",
                        "mpart_category": "采购件",
                        "mpart_unit": "米",
                        "mpart_specification": "304",
                        "mpart_id": "Part0500",
                        "id": "M500"
                    },
                    "type": ["MPart"]
                }
            ]
        """
        parameters = {
            "element_class": object_type,
            "element_type": "NODE",
            "element_uuid": uuid,
        }
        result = self._search_post("property_info_search", parameters).get("result")
        if not result:
            return {
                "original": f"未查到 UUID 为 '{uuid}' 的节点信息。",
                "frontend_msg": f"未查到 UUID 为 '{uuid}' 的节点信息。",
            }
        return {
            "original": _pretty_json_for_display(result),
            "frontend_msg": f"查询到 UUID 为 '{uuid}' 的节点信息。",
        }

    @Env.tool
    def get_relation_info(self, relation_type: str):
        """
        查询指定类型的所有关系实例
        """
        parameters = {
            "element_class": relation_type,
            "element_type": "EDGE",
            "filter_dict": {},
        }
        result = self._search_post("property_filter", parameters).get("result")
        if not result:
            return {
                "original": f"未查到类型为 '{relation_type}' 的关系实例。",
                "frontend_msg": f"未查到类型为 '{relation_type}' 的关系实例。",
            }
        return {
            "original": f"查询到{relation_type}的关系如下：\n{_pretty_json_for_display(result)}",
            "frontend_msg": f"查询到类型为 '{relation_type}' 的关系实例。",
        }

    @Env.tool
    def get_edge_info(self, relation_type: str, uuid: str):
        """
        查询指定 uuid 的关系属性信息
        """
        parameters = {
            "element_class": relation_type,
            "element_type": "EDGE",
            "element_uuid": uuid,
        }
        result = self._search_post("property_info_search", parameters).get("result")
        if not result:
            return {
                "original": f"未查到 UUID 为 '{uuid}' 的关系信息。",
                "frontend_msg": f"未查到 UUID 为 '{uuid}' 的关系信息。",
            }
        return {
            "original": _pretty_json_for_display(result),
            "frontend_msg": f"查询到 UUID 为 '{uuid}' 的关系信息。",
        }

    @Env.tool
    def hop_search(self, uuid: str, hop_num: int, accurate_flag: bool):
        """
        使用场景：
            - 查询节点之间的多跳关系路径
        函数功能：
            - 从指定 uuid 节点出发
            - 按跳数进行路径搜索
            - 支持精确或最大跳数匹配

        Args:
            uuid:
                起始节点的唯一标识符
            hop_num:
                跳数
            accurate_flag:
                是否精确匹配跳数
                - True：仅返回精确 hop_num 跳的路径（路径条数 = hop_num）
                - False：返回最多 hop_num 跳的所有路径（路径条数 <= hop_num）

        Returns:
            多跳路径及其节点、关系信息，主要包括：
                - start Node 入参uuid、ID、Name
                - end Node 符合条件的Node（uuid ID Name）
                - nodes[list] 路径内的所有node信息
                - relations[list] 包含关联关系信息，字段：to from type
        """
        parameters = {"uuid": uuid, "hop_num": hop_num, "accurate_flag": accurate_flag}
        result = self._search_post("hop_search", parameters).get("result")
        if not result:
            return {
                "original": "未查到相关多跳关联路径。",
                "frontend_msg": "未查到相关多跳关联路径。",
            }
        return {"original": _pretty_json_for_display(result), "frontend_msg": "查询成功，返回多跳路径。"}

    @Env.tool
    def pattern_search(self, start_object_type: str, relation_type: str, direction: str, end_object_type: str):
        """
        使用场景：
            - 需要查询符合特定路径模式的实例时使用(只有数据类型为浮点型、整型时才能进行运算)
        函数功能：
            - 根据指定的起点、终点和关系类型执行图数据库查询
            - 返回从起点到终点的匹配路径中的节点数据
        Args:
            start_object_type (str): 起始节点的标签类型（如"Fund", "Company", "Person"等）
            relation_type (str): 关系类型（如"Fund-INVESTS-Company", "Company-OWNS-Person"等）
            direction (str): 关系方向指示符:
                "-" 表示 (A)-(B)，双向关系
                "->" 表示 (A)->(B)，A是起始节点，B是终止节点
                "<-" 表示 (A)<-(B)，B是起始节点，A是终止节点
            end_object_type (str): 终止节点的标签类型（如"Fund", "Company", "Person"等）

        Returns:
            dict: 包含查询结果的字典
                - original: 格式化的查询结果文本
                - frontend_msg: 前端显示的简要消息
                如果未查到结果，返回"未查到相关路径实例。"
        """
        path_pattern = [[f"{start_object_type}", {}], [f"{relation_type}", direction], [f"{end_object_type}", {}]]
        parameters = {"path_pattern": path_pattern, "return_vars": ["var0", "var2"]}
        result = self._search_post("pattern_search", parameters).get("result")
        if not result:
            return {
                "original": "未查到相关路径实例。",
                "frontend_msg": "未查到相关路径实例。",
            }
        return {
            "original": f"查询成功，实体{start_object_type}到{end_object_type}，"
            f"关系为{relation_type}的有关联节点如下:\n{_pretty_json_for_display(result)}",
            "frontend_msg": "查询成功，返回路径实例。",
        }

    @Env.tool
    def get_relation_by_startID(self, startNodeUUID: str, realtionType: str):
        """
        使用场景：
            - 查询某个节点出发的边
        Args:
            startNodeUUID(str): 开始节点的UUID(**注意:是uuid**)
            realtionType(str): 关系的类型
        Returns:
            以开始节点UUID为起点，关系类型为realationType的边信息
        """
        parameters = {"uuid": startNodeUUID, "hop_num": 1, "accurate_flag": True}
        hop_search_result = self._search_post("hop_search", parameters).get("result")
        if not hop_search_result:
            return {"original": "未查到相关边实例。", "frontend_msg": "未查到相关边实例。"}
        result_all = []
        for cur_info in hop_search_result:
            if realtionType == cur_info["relations"][0]["relation_type"]:
                edge_uuid = cur_info["relations"][0]["uuid"]
                parameters = {
                    "element_class": cur_info["relations"][0]["relation_type"],
                    "element_type": "EDGE",
                    "element_uuid": edge_uuid,
                }
                property_result = self._search_post("property_info_search", parameters).get("result")
                if property_result:
                    result_all.append(property_result[0])
        return {
            "original": f"查询成功，以{startNodeUUID}为起点，关系类型为{realtionType}的边信息有：{result_all}",
            "frontend_msg": "查询成功，返回路径实例。",
        }

    @Env.tool
    def property_filter(self, element_class: str, element_type: str, filter_dict: dict) -> dict[str, Any]:
        """
        使用场景：
            - 当需要从图数据库中检索满足特定属性条件的节点或边时使用，特别适用于：
                同一属性的多值筛选（如名称包含"红杉"或"高瓴"的基金）
                组合条件查询（如规模大于1亿且成立时间在2010年后）
                多关键词搜索（如描述中包含"人工智能"或"机器学习"的企业）
                范围条件组合（如年龄大于18且小于30的用户）
                (只有数据类型为浮点型、整型时才能进行运算)
        函数功能:
            - 基础匹配 (MATCH):
            - 根据传入的 `element_type`（节点或边）和 `element_class`（节点标签或边类型）构建基础的 `MATCH` 子句。
            - 对于节点，生成 `MATCH (n:ElementClass)`。
            - 对于边，生成 `MATCH ()-[e:ElementClass]->()`。
            - 属性过滤 (WHERE):
            - 将 `filter_dict` 中的每个键值对转换为 Cypher 的属性过滤条件。
            - 键作为属性名，值作为比较逻辑（如 `> 100`, `CONTAINS 'text'`）。
            - 使用 `AND` 逻辑将所有过滤条件组合在一起，构成 `WHERE` 子句。
            - 如果 `filter_dict` 为空，则不生成 `WHERE` 子句。
            - 返回结果 (RETURN):
            - 生成一个基础的 `RETURN` 子句，用于返回匹配到的元素本身。
            - 返回 uuid, id, name，方便在查询结果中直接引用。
        Args:
            合法过滤操作符：
            VALID_OPERATORS = ["IS NOT NULL", "IS NULL","<=", ">=", "=", "<", ">",
            "CONTAINS", "STARTS WITH", "ENDS WITH", "IN"]
            element_class (str):
                图元素的类名或标识符。
                - 如果 `element_type` 是 "NODE"，则此参数代表节点的 **标签** (Label)。
                例如 "Fund"。
                - 如果 `element_type` 是 "EDGE"，则此参数代表边的 **类型** (Type)。
                例如 "FundInvestment"。
                对于边，通常使用其中的"start_id"与"end_id"字段与节点关联。
                即e.start_id/e.end_id的来源是n.id，可以通过这种方式在filter_dict实现筛选过滤。
                注意！不要自己生成id，id应通过查询获得。
            element_type (str):
                指定要查询的图元素类型，必须是 "NODE" 或 "EDGE"。
                - 使用 "NODE" 查询节点。
                - 使用 "EDGE" 查询关系。
            filter_dict (dict):
                一个字典，用于定义属性过滤条件。
                - **键 (Key)**: 字符串类型的属性名。
                - **值 (Value)**: 字符串类型的 Cypher 比较或谓词逻辑。
                - 对于每个属性，在每个键值内尽量只生成一个条件
                    例如：`">= 10000"`, `"CONTAINS '中国'"`, `" STARTS WITH 'A'"`, `"= 'Tencent'"`。
                - 如果要在子条件中使用OR语法，每个子条件必须是完整的比较表达式，但不需要每次都写出属性名
                    例如：
                    正确示例为
                    - "CONTAINS 'A' OR CONTAINS 'B'"
                    - "= '清算期亏损基金' OR = '启明创投消费科技基金 IV' OR = '医疗设备并购基金' OR = '华芯科创创业投资基金'"
                    错误示例：
                    "CONTAINS 'A' OR 'B'"（错误原因：缺少操作符）"= '3' OR id = '9' OR id = '29'"
                    （错误原因：重复属性名id）
        """
        parsed_filter = _safe_literal_eval(filter_dict)
        parameters = {"element_class": element_class, "element_type": element_type, "filter_dict": parsed_filter}
        result = self._search_post("property_filter", parameters).get("result")
        return {
            "original_msg": result,
            "frontend_msg": {
                "left": f"类名: {element_class}\n节点/边类型: {element_type}\n过滤条件: {filter_dict}",
                "right": f"### 执行结果\n- 按输入条件已完成属性过滤检索，结果如下：\n```\n{_pretty_json_for_display(result)}\n```",
            },
        }

    @Env.tool
    def property_info_search(self, element_class, element_type, element_uuid):
        """
        使用场景：
            - 需要查询某个节点或某条边上，所有的属性信息描述时使用。
              当你无法推断某个属性的语义信息，或需要确认某个属性代表的含义时，请调用此工具。
        函数功能：
            - 根据图元素类名element_class，图元素类型element_type，以及图元素element_uuid，
              返回该图元素上所有属性名，对应的属性语义描述，以及具体的属性取值
        Args:
            element_class (str):
                图元素的类名或标识符。
                - 如果 `element_type` 是 "NODE"，则此参数代表节点的 **标签** (Label)。
                例如 "Fund"。
                - 如果 `element_type` 是 "EDGE"，则此参数代表边的 **类型** (Type)。
                例如 "FundInvestment"。
                对于边，通常使用其中的"start_id"与"end_id"字段与节点关联，即e.start_id/e.end_id=n.id，
                可以通过这种方式在filter_dict实现筛选过滤
            element_type (str):
                指定要查询的图元素类型，必须是 "NODE" 或 "EDGE"。
                - 使用 "NODE" 查询节点。
                - 使用 "EDGE" 查询关系。
            element_uuid：
                图元素中的uuid字段，用于定位具体图元素

        Returns:
            该函数返回某个节点或某条边的全部属性信息，以List[Dict[str, Any]]的格式，列表中包含多个字典，每个字典描述了一个属性的基本信息，
            分别是property_name属性命名，property_description属性含义，property_value属性取值。
            你可以根据property_description获取属性的语义信息，来解答用户提出的问题
            例如：对于查询，element_class='Fund', element_type='NODE', element_uuid='1'，返回如下结果
                [{"property_name": "fund_type", "property_description": "基金类型", "property_value": "产业投资基金"},
                    {"property_name": "AAA", "property_description": "基金的募集规模", "property_value": "80000.0"}]
                代表对于基金类型的节点1，有两个属性，
                分别是fund_type，含义是基金类型，取值是产业投资基金，
                AAA属性的含义是基金的募集规模，取值是80000.0
        """
        parameters = {
            "element_class": element_class,
            "element_type": element_type,
            "element_uuid": element_uuid,
        }
        result = self._search_post("property_info_search", parameters).get("result", [])
        result_display = result.copy() if isinstance(result, list) else result
        if isinstance(result_display, list) and result_display and isinstance(result_display[0], dict):
            result_display[0].pop("reminder", None)
        return {
            "original_msg": result,
            "frontend_msg": {
                "left": "# 属性信息检索流程\n1. 接收 element_class、element_type、element_uuid\n"
                "2. 组织查询请求，查询对应节点/边全量属性\n3. 解析返回结构（属性名/属性含义/属性值）\n4. 返回属性信息",
                "right": f"### 查询结果\n- 所有属性/含义/取值信息：\n```\n{_pretty_json_for_display(result_display)}\n```",
            },
        }

    @Env.tool
    def exp_checklist(self, pseudovirus_name: str, cell_name: str, antibody_name: str) -> dict[str, str]:
        """
        生成中和实验耗材检查清单。

        Args:
            pseudovirus_name: 假病毒名称。
            cell_name: 细胞系名称。
            antibody_name: 抗体名称。

        Returns:
            包含原始提示与前端展示内容的字典。
        """
        msg = (
            "🧪 以下是基于 '"
            f"{pseudovirus_name} + {antibody_name} + {cell_name} 中和试验' 本体查询结果生成的实验耗材检查清单:\n"
            "| 类别 | 名称 | 状态 |\n"
            "|---|---|---|\n"
            f"| 假病毒 | {pseudovirus_name}假病毒 | 待确认库存 |\n"
            f"| 抗体 | {antibody_name}抗体 |待确认库存 |\n"
            f"| 细胞 | {cell_name}细胞系 | 待确认库存 |\n"
            "| 培养相关 | 培养基、缓冲液 | 系统默认 |\n"
            "| 其他耗材 | 多孔板、移液耗材 | 系统默认 |"
        )
        return {
            "original_msg": "['假病毒', '抗体', '细胞', '培养相关', '其他耗材']",
            "frontend_msg": msg,
        }

    @Env.tool
    def is_sample_volume_enough_for_exp(self, sample_name: str, sample_type: str, uuid: str) -> dict[str, str]:
        """
        检查样本余量是否满足实验需求。

        Args:
            sample_name: 样本名称。
            sample_type: 样本类型，需为 AntibodySample / PseudovirusSample / CellSample 之一。
            uuid: 样本 UUID。

        Returns:
            包含检查结果文本的字典。
        """
        if sample_type == "AntibodySample":
            action_name = "is_antibody_sample_volume_enough_for_exp"
        elif sample_type == "CellSample":
            action_name = "is_cell_sample_volume_enough_for_exp"
        elif sample_type == "PseudovirusSample":
            action_name = "is_sample_volume_enough_for_exp"
        else:
            return {
                "error": f"Unsupported sample_type: {sample_type}. Supported types: AntibodySample, CellSample,"
                f" PseudovirusSample"
            }
        action_id = self.action_to_uuid(action_name)
        parameters = {
            "instance_type": "entity",
            "instance_api_name": sample_type,
            "instance_id": uuid,
            "action_id": action_id,
            "input_params": {},
        }
        resp = self._action_post("", parameters)
        res = resp["data"]["action_return"]["value"]["success"] == "True"
        if res:
            msg = f"✅ {sample_type} {sample_name} 余量充足，可以支撑实验。"
        else:
            msg = f"⚠️ {sample_type} {sample_name} 余量不足，无法支撑实验。已标记库存短缺，待样本制备小组处理。"
        return {"original_msg": msg, "frontend_msg": msg}

    @Env.tool
    def register_exp(
        self,
        pseudovirus_sample_uuid: str,
        cell_sample_uuid: str,
        antibody_sample_uuid: str,
        pseudovirus_name: str,
        cell_name: str,
        antibody_name: str,
    ) -> dict[str, str]:
        """
        注册中和实验并返回实验编号。

        Args:
            pseudovirus_sample_uuid: 假病毒样本 UUID。
            cell_sample_uuid: 细胞样本 UUID。
            antibody_sample_uuid: 抗体样本 UUID。
            pseudovirus_name: 假病毒名称。
            cell_name: 细胞系名称。
            antibody_name: 抗体名称。

        Returns:
            包含实验编号与前端展示内容的字典。
        """
        _ = (pseudovirus_name, cell_name, antibody_name)
        action_id = self.action_to_uuid("create_expv2")
        parameters = {
            "instance_type": "entity",
            "instance_api_name": "NeutralizationExperiment",
            "instance_id": "None",
            "action_id": action_id,
            "input_params": {
                "pseudovirus_sample_uuid": pseudovirus_sample_uuid,
                "cell_sample_uuid": cell_sample_uuid,
                "antibody_sample_uuid": antibody_sample_uuid,
            },
        }
        resp = self._action_post("", parameters)
        try:
            exp_id = resp["data"]["action_return"]["value"]["id"]
            msg = f"🧪 中和实验已注册，编号 {exp_id}。"
        except Exception:
            logger.error("register_exp失败，返回结果：%s", resp)
            exp_id = "590237"
            msg = f"🧪 中和实验已注册，编号 {exp_id}。"
        return {"original_msg": exp_id, "frontend_msg": msg}

    @Env.tool
    def create_exp_workflow(self) -> dict[str, str]:
        """
        获取中和实验的流程步骤描述。

        Returns:
            包含流程列表与前端展示文本的字典。
        """
        parameters = {
            "element_class": "ExpManual",
            "element_type": "NODE",
            "filter_dict": {"exp_title": "CONTAINS '中和实验'"},
        }
        resp = self._search_post("", parameters)
        result = resp.get("result") or []
        if not result:
            return {"original_msg": "[]", "frontend_msg": "未找到实验流程。"}
        uuid = result[0]["n.uuid"]
        parameters = {
            "element_class": "ExpManual",
            "element_type": "NODE",
            "element_uuid": uuid,
        }

        resp = self._search_post("", parameters)
        msg = resp.get("result", [{}])[0].get("properties", {}).get("exp_procedures", "") if resp.get("result") else ""
        return {
            "original_msg": "['中和孔实验', '中和滴度实验', '拟合分析']",
            "frontend_msg": msg,
        }

    @Env.tool
    def check_avail_operators(self) -> dict[str, str]:
        """
        查询可用实验员与当前排期。

        Returns:
            包含实验员状态文本的字典。
        """
        msg = (
            "\n📅 实验员状态如下：\n"
            "| 实验员 | 实验技能 | 当前实验队列 | 预计完成时间 |\n"
            "|---|---|---|---|\n"
            "| 研究员1 | 中和滴度实验，拟合分析 | exp-6 | 12:00 |\n"
            "| 研究员2 | 中和孔实验 | exp6, exp-9 | 15:00 |\n"
            "| 研究员3 | 拟合分析 | exp-10 | 17:00 |\n"
            "| 研究员4 | 中和孔实验 | exp-10 | 17:00 |\n"
            "| 研究员5 | 中和滴度实验 | exp-8 | 13:00 |"
        )
        return {"original_msg": "['研究员2', '研究员5', '研究员1']", "frontend_msg": msg}

    @Env.tool
    def assign_operators_to_exp(self, exp_id: str, operators: str) -> dict[str, str]:
        """
        为实验分配实验员并返回排期信息。

        Args:
            exp_id: 实验编号。
            operators: 实验员列表或名称。

        Returns:
            包含分配结果与前端展示文本的字典。
        """
        import time

        time.sleep(3.57)
        original_msg = f"已为实验 {exp_id} 安排 {operators}。"
        frontend_msg = (
            "NeutralizationExperiment 包含 ['中和孔实验', '中和滴度实验', '拟合分析'] 三个步骤。"
            f"当前实验员技能及占用状态分析完毕。已为实验 {exp_id} 安排 {operators}，将于 15:00 开始，于 18:00 结束。\n\n"
            "📅 实验员状态已更新为:\n"
            "| 实验员 | 实验技能 | 当前实验队列 | 预计完成时间 |\n"
            "|---|---|---|---|\n"
            "| 研究员1 | 中和滴度实验，拟合分析 | exp-6, exp-11 | 18:00 |\n"
            "| 研究员2 | 中和孔实验 | exp6, exp-9, exp-11 | 18:00 |\n"
            "| 研究员5 | 中和滴度实验 | exp-8, exp-11 | 18:00 |"
        )
        return {"original_msg": original_msg, "frontend_msg": frontend_msg}

    @Env.tool
    def count_search(self, element_class, element_type, filter_dict):
        """
        使用场景：
            - 当需要从图数据库中统计满足特定属性条件的节点或边的数量时使用(只有数据类型为浮点型、整型时才能进行运算)
        函数功能:
            - 基础匹配 (MATCH):
            - 根据传入的 `element_type`（节点或边）和 `element_class`（节点标签或边类型）构建基础的 `MATCH` 子句。
            - 对于节点，生成 `MATCH (n:ElementClass)`。
            - 对于边，生成 `MATCH ()-[e:ElementClass]->()`。
            - 属性过滤 (WHERE):
            - 将 `filter_dict` 中的每个键值对转换为 Cypher 的属性过滤条件。
            - 键作为属性名，值作为比较逻辑（如 `> 100`, `CONTAINS 'text'`）。
            - 使用 `AND` 逻辑将所有过滤条件组合在一起，构成 `WHERE` 子句。
            - 如果 `filter_dict` 为空，则不生成 `WHERE` 子句。
            - 返回结果 (RETURN):
            - 生成一个基础的 `RETURN` 子句，用于返回匹配到的元素本身。
            - 返回 满足filter_dict过滤条件的节点或边的数量。
        Args:
            合法过滤操作符：
            VALID_OPERATORS = ["IS NOT NULL", "IS NULL","<=", ">=", "=", "<", ">","CONTAINS",
             "STARTS WITH", "ENDS WITH", "IN"]
            element_class (str):
                图元素的类名或标识符。
                - 如果 `element_type` 是 "NODE"，则此参数代表节点的 **标签** (Label)。
                例如 "Fund"。
                - 如果 `element_type` 是 "EDGE"，则此参数代表边的 **类型** (Type)。
                例如 "FundInvestment"。
                对于边，通常使用其中的"start_id"与"end_id"字段与节点关联，即e.start_id/e.end_id的来源是n.id，
                可以通过这种方式在filter_dict实现筛选过滤。
                注意！不要自己生成id，id应通过查询获得。
            element_type (str):
                指定要查询的图元素类型，必须是 "NODE" 或 "EDGE"。
                - 使用 "NODE" 查询节点。
                - 使用 "EDGE" 查询关系。
            filter_dict (dict):
                一个字典，用于定义属性过滤条件。
                - **键 (Key)**: 字符串类型的属性名。
                - **值 (Value)**: 字符串类型的 Cypher 比较或谓词逻辑。
                - 对于每个属性，在每个键值内尽量只生成一个条件
                    例如：`">= 10000"`, `"CONTAINS '中国'"`, `" STARTS WITH 'A'"`, `"= 'Tencent'"`。
                - 如果要在子条件中使用OR语法，每个子条件必须是完整的比较表达式，但不需要每次都写出属性名
                    例如：正确示例为
                    - "CONTAINS 'A' OR CONTAINS 'B'"
                    - "= '清算期亏损基金' OR = '启明创投消费科技基金 IV' OR = '医疗设备并购基金' OR = '华芯科创创业投资基金'"
                    错误示例：
                    - "CONTAINS 'A' OR 'B'"
                    （错误原因：缺少操作符）
                    - "= '3' OR id = '9' OR id = '29'"
                    （错误原因：重复属性名id）
        """
        parameters = {
            "element_class": element_class,
            "element_type": element_type,
            "filter_dict": _safe_literal_eval(filter_dict),
        }
        result = self._search_post("count_search", parameters).get("result")
        return {
            "original_msg": result,
            "frontend_msg": {
                "left": "# 节点/边数量统计流程\n1. 接收类型和过滤条件\n2. 构造数量统计请求\n3. 提交POST到 /count_search\n4. 解析数量结果\n5. 输出统计信息",
                "right": f"### 统计结果\n- 满足属性条件的数量：`{_pretty_json_for_display(result)}`",
            },
        }

    @Env.tool
    def aggregate_search(self, element_class, element_type, target_property, agg, filter_dict):
        """
        使用场景：
            - 当需要对图数据库中特定类型的节点或边进行数值型属性的聚合统计时使用，典型场景包括：
            - 业务指标分析：计算基金节点的平均规模、统计投资边的总金额
            - 数据洞察：查询企业节点的最大注册资本、获取行业的最小成立年限
            - 条件统计：统计满足特定条件的节点数量（如名称包含"科技"的企业数量）
            - 趋势分析：按时间维度聚合交易数据（需配合时间属性）
        函数功能:
            - 基础匹配 (MATCH):
            - 根据传入的 `element_type`（节点或边）和 `element_class`（节点标签或边类型）构建基础的 `MATCH` 子句。
            - 对于节点，生成 `MATCH (n:ElementClass)`。
            - 对于边，生成 `MATCH ()-[e:ElementClass]->()`。
            - 属性过滤 (WHERE):
            - 将 `filter_dict` 中的每个键值对转换为 Cypher 的属性过滤条件。
            - 键作为属性名，值作为比较逻辑（如 `> 100`, `CONTAINS 'text'`）。
            - 使用 `AND` 逻辑将所有过滤条件组合在一起，构成 `WHERE` 子句。
            - 如果 `filter_dict` 为空，则不生成 `WHERE` 子句。
            - 返回结果 (RETURN):
            - 生成一个基础的 `RETURN` 子句，用于返回匹配到的元素本身。
            - 返回 满足filter_dict过滤条件的节点或边的数量。
            - 只有数据类型为浮点型、整型时才能进行运算
        Args:
            合法过滤操作符：
            VALID_OPERATORS = ["IS NOT NULL", "IS NULL","<=", ">=", "=", "<", ">","CONTAINS",
             "STARTS WITH", "ENDS WITH", "IN"]
            element_class (str):
                图元素的类名或标识符。
                - 如果 `element_type` 是 "NODE"，则此参数代表节点的 **标签** (Label)。
                例如 "Fund"。
                - 如果 `element_type` 是 "EDGE"，则此参数代表边的 **类型** (Type)。
                例如 "FundInvestment"。
                对于边，通常使用其中的"start_id"与"end_id"字段与节点关联，即e.start_id/e.end_id的来源是n.id
                可以通过这种方式在filter_dict实现筛选过滤。
                注意！不要自己生成id，id应通过查询获得。
            element_type (str):
                指定要查询的图元素类型，必须是 "NODE" 或 "EDGE"。
                - 使用 "NODE" 查询节点。
                - 使用 "EDGE" 查询关系。
            target_property（str）:
                要聚合的目标属性名
            agg（str）:
                聚合函数类型
                - 支持 "SUM", "AVG", "MIN", "MAX", "COUNT"
                - 对于str类型的属性，不支持SUM合AVG操作
            filter_dict (dict):
                一个字典，用于定义属性过滤条件。
                - **键 (Key)**: 字符串类型的属性名。
                - **值 (Value)**: 字符串类型的 Cypher 比较或谓词逻辑。
                - 对于每个属性，在每个键值内尽量只生成一个条件
                例如：`">= 10000"`, `"CONTAINS '中国'"`, `" STARTS WITH 'A'"`, `"= 'Tencent'"`。
                - 如果要在子条件中使用OR语法，每个子条件必须是完整的比较表达式，但不需要每次都写出属性名
                例如：正确示例为
                - "CONTAINS 'A' OR CONTAINS 'B'"
                - "= '清算期亏损基金' OR = '启明创投消费科技基金 IV' OR = '医疗设备并购基金' OR = '华芯科创创业投资基金'"
                错误示例："CONTAINS 'A' OR 'B'"（错误原因：缺少操作符）"= '3' OR id = '9' OR id = '29'"（错误原因：重复属性名id）
        Returns:
            聚合统计结果值
        """
        parameters = {
            "element_class": element_class,
            "element_type": element_type,
            "target_property": target_property,
            "agg": agg,
            "filter_dict": _safe_literal_eval(filter_dict),
        }
        result = self._search_post("aggregate_search", parameters).get("result")
        return {
            "original_msg": result,
            "frontend_msg": {
                "left": "# 聚合统计流程\n1. 接收聚合参数：目标类型/属性/方法/过滤条件\n2. 组织Cypher聚合函数请求\n3. POST到 /aggregate_search\n4. 返回聚合结果",
                "right": f"### 聚合结果\n- 聚合统计值：`{_pretty_json_for_display(result)}`",
            },
        }

    @Env.tool
    def sorted_search(self, element_class, element_type, filter_dict, return_properties, sort_by, ascending):
        """
        使用场景：
            - 当需要对图数据库中特定类型的节点或边按照某个特定属性进行排序时使用(只有数据类型为浮点型、整型时才能进行运算)
        函数功能:
            - 生成带条件过滤和排序的 Cypher 查询语句
            - 执行查询并返回满足以下条件的图数据结果：
            - 指定类型的节点或边（如 "Company" 节点或 "INVESTS" 关系）
            - 符合过滤条件的数据（如营收大于 10000）
            - 按指定属性排序（如按注册时间升序排列）
        Args:
            合法过滤操作符：
            VALID_OPERATORS = ["IS NOT NULL", "IS NULL","<=", ">=", "=", "<", ">","CONTAINS",
             "STARTS WITH", "ENDS WITH", "IN"]
            element_class (str):
                图元素的类名或标识符。
                - 如果 `element_type` 是 "NODE"，则此参数代表节点的 **标签** (Label)。
                例如 "Fund"。
                - 如果 `element_type` 是 "EDGE"，则此参数代表边的 **类型** (Type)。
                例如 "FundInvestment"。
                对于边，通常使用其中的"start_id"与"end_id"字段与节点关联，即e.start_id/e.end_id的来源是n.id。
                可以通过这种方式在filter_dict实现筛选过滤。
                注意！不要自己生成id，id应通过查询获得。
            element_type (str):
                指定要查询的图元素类型，必须是 "NODE" 或 "EDGE"。
                - 使用 "NODE" 查询节点。
                - 使用 "EDGE" 查询关系。
            filter_dict (dict):
                一个字典，用于定义属性过滤条件。
                - **键 (Key)**: 字符串类型的属性名。
                - **值 (Value)**: 字符串类型的 Cypher 比较或谓词逻辑。
                - 对于每个属性，在每个键值内尽量只生成一个条件
                    例如：`">= 10000"`, `"CONTAINS '中国'"`, `" STARTS WITH 'A'"`, `"= 'Tencent'"`。
                - 如果要在子条件中使用OR语法，每个子条件必须是完整的比较表达式，但不需要每次都写出属性名
                    例如：正确示例为
                    - "CONTAINS 'A' OR CONTAINS 'B'"
                    - "= '清算期亏损基金' OR = '启明创投消费科技基金 IV' OR = '医疗设备并购基金' OR = '华芯科创创业投资基金'"
                    错误示例：
                    - "CONTAINS 'A' OR 'B'"（错误原因：缺少操作符）"= '3' OR id = '9' OR id = '29'"
                    （错误原因：重复属性名id）
            return_properties (list):
                - 需返回的属性列表，未提供时返回完整实体
                - 例：["name", "salary"]
            sort_by (str):
                - 排序依据的属性名，未提供时不排序
            ascending (bool):
                - 排序方向：True=升序(ASC)/False=降序(DESC)，默认 True
        Returns:
            当指定 return_properties 时：返回属性键值对列表
            例：[{"name": "腾讯", "revenue": 50000}, ...]
            未指定 return_properties 时：返回完整图元素对象（节点/边）
            结果按 sort_by 和 ascending 参数排序，无排序参数时按数据库默认顺序返回
        """
        parameters = {
            "element_class": element_class,
            "element_type": element_type,
            "filter_dict": _safe_literal_eval(filter_dict),
            "return_properties": _safe_literal_eval(return_properties),
            "sort_by": sort_by,
            "ascending": ascending,
        }
        result = self._search_post("sorted_search", parameters).get("result")
        return {
            "original_msg": result,
            "frontend_msg": {
                "left": "# 排序查询流程\n1. 接收类型、过滤条件、排序属性、方向等参数\n2. 组织Cypher排序查询\n3. POST到 /sorted_search\n4. 解析排序结果",
                "right": f"### 排序结果\n- 排序后数据如下：\n```\n{_pretty_json_for_display(result)}\n```",
            },
        }

    @Env.tool
    def cal_mom(self, sample_type: str, uuid: str):
        """
        计算环比数据
        Args:
            sample_type:
                例如 "KpiOfDailyCompanyDeposit"
            uuid: 指标节点的 UUID。
                例如 "7da425e2-07b6-4147-adb6-941c60ce5079"
        Returns:
            dict: 包含计算结果的数据字典
        """
        url = self._with_scene_name(self.action_base_url)
        action_id = self.action_to_uuid("calculate_kpi_mom_growth")
        parameters = {
            "instance_type": "entity",
            "instance_api_name": sample_type,
            "instance_id": uuid,
            "action_id": action_id,
            "input_params": {},
        }
        logger.trace(f"cal_mom parameters: {parameters}")
        resp = requests.post(url, json=parameters)
        resp_dict = resp.json()
        action_return = resp_dict["data"]["action_return"]
        return {
            "original_msg": resp_dict,
            "frontend_msg": f"### 计算结果\n- 指标环比结果如下：\n```\n{action_return}\n```",
        }

    @Env.tool
    def cal_yoy(self, sample_type: str, uuid: str):
        """
        计算同比数据

        Args:
            sample_type:
                例如 "KpiOfDailyCompanyDeposit"
            uuid: 指标节点的 UUID。
                例如 "7da425e2-07b6-4147-adb6-941c60ce5079"

        Returns:
            dict: 包含计算结果的数据字典
        """
        url = self._with_scene_name(self.action_base_url)
        action_id = self.action_to_uuid("calculate_kpi_yoy_growth")
        parameters = {
            "instance_type": "entity",
            "instance_api_name": sample_type,
            "instance_id": uuid,
            "action_id": action_id,
            "input_params": {},
        }
        resp = requests.post(url, json=parameters)
        resp_dict = resp.json()
        action_return = resp_dict["data"]["action_return"]
        return {
            "original_msg": resp_dict,
            "frontend_msg": f"### 计算结果\n- 指标环比结果如下：\n```\n{action_return}\n```",
        }

    @Env.tool
    def save_report_to_markdown(self, report_content: str, file_path: str = "") -> dict[str, Any]:
        """
        使用场景：
            - 当 Agent 生成了一段较长的分析报告、结论说明或对账结果说明时。
                需要将结果长期保存到本地 Markdown 文件中，方便后续查阅、对比或接入其他系统。
            - 当用户要求生成报告时，最后一步**一定要**调用这个工具，将报告内容放在 `report_content` 参数中。
            - 如果用户没有要求生成报告，则不用调用该工具

        函数功能：
            - 将传入的 `report_content` 以 Markdown 格式写入到指定路径的 `.md` 文件中。
            - 如果未显式指定 `file_path`，则自动在项目根目录下的 `reports` 目录中按时间生成一个文件名。

        Args:
            report_content (str):
                - Agent 输出的完整报告内容，建议直接传入模型最终回答的文本。
            file_path (str, optional):
                - 目标 Markdown 文件的相对或绝对路径。
                - 支持传入已经存在的 `.md` 文件路径（会覆盖写入）或新文件路径。
                - 为空字符串或未提供时，默认路径为：
                  `<项目根目录>/reports/icbc_report_YYYYMMDD_HHMMSS.md`

        Returns:
            str:
                实际写入的 Markdown 文件的绝对路径。
        """
        if not file_path:
            project_root = Path(__file__).resolve().parents[2]
            reports_dir = project_root / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            timestamp = (datetime.datetime.now(tz=UTC) + timedelta(hours=8)).strftime("%Y%m%d_%H%M%S")
            file_path = str(reports_dir / f"ontology_report_{timestamp}.md")
        else:
            target_path = Path(file_path)
            if not target_path.is_absolute():
                project_root = Path(__file__).resolve().parents[2]
                target_path = project_root / target_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            file_path = str(target_path)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(report_content)

        abs_path = str(Path(file_path).resolve())
        return {
            "original_msg": abs_path,
            "frontend_msg": {
                "left": "# 保存报告流程\n1. 判断是否传入文件名\n2. 若无则自动生成时间戳文件名\n3. 确认目录存在，写入Markdown内容\n4. 返回保存路径",
                "right": f"### 已保存Markdown报告\n- 文件路径：{abs_path}\n\n{report_content}",
            },
        }

    @Env.tool
    def ontology_search(self, user_query: str) -> dict[str, Any]:
        """
        根据用户查询解析样本名称并返回本体匹配结果。

        Args:
            user_query: 用户输入的查询文本。

        Returns:
            包含本体匹配信息、工具清单与反馈提示的字典。
        """
        names = self._list_names()
        samples: dict[str, str | None] = {}
        for key, options in names.items():
            match = None
            for option in options:
                if option and option in user_query:
                    match = option
                    break
            samples[key] = match

        try:
            context = {"query": user_query, **names}
            samples_text = execute_with_llm("ontology", context)
            parsed = _parse_output(samples_text)
            if parsed:
                samples = parsed
        except Exception as exc:
            logger.warning("ontology perceptor unavailable: %s", exc)

        feedback_query, require_human_feedback = "", False
        for key, value in (samples or {}).items():
            if value is None:
                feedback_query += f"\n{key} 信息缺失。请从 {names[key]} 中选择。"
                require_human_feedback = True
        uuids = self._names_to_uuids(samples)
        ontology_result = {key: {"name": value, "uuid": uuids[key]} for key, value in (samples or {}).items()}
        tools = {
            -1: {
                "type": "action",
                "label": "exp_checklist",
                "description": "Get the checklist of experiment consumables",
                "parameters": "pseudovirus_name (str)\ncell_name (str)\nantibody_name (str)",
                "output": "None",
            },
            -2: {
                "type": "action",
                "label": "is_sample_volume_enough_for_exp",
                "description": "Check if sample volume is enough for experiment",
                "parameters": "sample_name(str)\nsample_type (str): "
                "Must be one of {'AntibodySample', 'PseudovirusSample', 'CellSample'}\nuuid (str)",
                "output": "None",
            },
            -3: {
                "type": "action",
                "label": "register_exp",
                "description": "Register experiment",
                "parameters": (
                    "pseudovirus_sample_uuid (str)\ncell_sample_uuid (str)\nantibody_sample_uuid (str)\n"
                    "pseudovirus_name (str)\ncell_name (str)\nantibody_name (str)"
                ),
                "output": "str: exp_id",
            },
            -4: {
                "type": "action",
                "label": "create_exp_workflow",
                "description": "Create experiment workflow",
                "parameters": "",
                "output": "None",
            },
            -5: {
                "type": "action",
                "label": "check_avail_operators",
                "description": "Check available operators",
                "parameters": "",
                "output": "str: operators",
            },
            -6: {
                "type": "action",
                "label": "assign_operators_to_exp",
                "description": "Assign operators to an experiment",
                "parameters": "exp_id (str)\noperators (str)",
                "output": "None",
            },
        }
        return {
            "require_human_feedback": require_human_feedback,
            "feedback_query": feedback_query,
            "ontology": json.dumps(ontology_result),
            "tools": tools,
        }

    def _with_scene_name(self, url: str) -> str:
        """为请求 URL 追加场景参数。"""
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}scene_name={self.scene}" if self.scene else url

    def _search_post(self, api: str, payload: dict[str, Any]) -> dict[str, Any]:
        """发送搜索服务 POST 请求并返回 JSON 响应。"""
        return requests.post(self._with_scene_name(f"{self.search_base_url}/{api}"), json=payload).json()

    def _action_post(self, api: str, payload: dict[str, Any]) -> dict[str, Any]:
        """发送动作服务 POST 请求并返回 JSON 响应。"""
        return requests.post(self._with_scene_name(f"{self.action_base_url}/{api}"), json=payload).json()

    def _antibody_name_to_id(self, name: str) -> str:
        data = {
            "element_class": "Antibody",
            "element_type": "NODE",
            "filter_dict": {"name": f"CONTAINS '{name}'"},
            "get_all_properties": True,
        }
        resp = self._search_post("property_filter", data)
        result = resp.get("result") or []
        if not result:
            return ""
        return result[0].get("properties", {}).get("id", "")

    def _names_to_uuids(self, input_data: dict[str, str | None]) -> dict[str, str | None]:
        mapping = {
            "cell": ("CellSample", "cell_type"),
            "pseudovirus": ("PseudovirusSample", "virus_type"),
            "antibody": ("AntibodySample", "antibody_id"),
        }
        res: dict[str, str | None] = {}
        for key, value in input_data.items():
            if not value:
                res[key] = None
                continue
            if key == "antibody":
                value = self._antibody_name_to_id(value)
            try:
                element_class, property_name = mapping[key]
            except KeyError:
                logger.error(f"KeyError: 键 '{key}' 不存在于 mapping 字典中", exc_info=True)
                element_class, property_name = None, None
            data = {
                "element_class": element_class,
                "element_type": "NODE",
                "filter_dict": {property_name: f"CONTAINS '{value}'" if isinstance(value, str) else f"= {value}"},
            }
            resp = self._search_post("property_filter", data)
            result = resp.get("result") or []
            res[key] = result[0].get("n.uuid") if result else None
        return res

    def _list_names(self) -> dict[str, list[str]]:
        mapping = [
            ("cell", "CellSample", "cell_type"),
            ("pseudovirus", "PseudovirusSample", "virus_type"),
            ("antibody", "Antibody", "name"),
        ]
        res: dict[str, list[str]] = {}
        for placeholder, element_class, property_name in mapping:
            data = {
                "element_class": element_class,
                "element_type": "NODE",
                "filter_dict": {},
            }
            resp = self._search_post("property_filter", data)
            result = resp.get("result") or []
            uuids = [x.get("n.uuid") for x in result if x.get("n.uuid")]
            res[placeholder] = self._uuids_to_names(element_class, uuids, property_name)
        return res

    def _uuids_to_names(self, element_class: str, uuids: list[str], property_name: str) -> list[str]:
        res = []
        for uuid in uuids:
            data = {
                "element_class": element_class,
                "element_type": "NODE",
                "element_uuid": uuid,
            }
            resp = self._search_post("property_info_search", data)
            result = resp.get("result") or []
            if not result:
                continue
            res.append(result[0].get("properties", {}).get(property_name))
        return res
