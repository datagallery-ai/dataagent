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
from typing import Any

import requests

from dataagent.actions.environment.env import Env

_MAX_BUSINESS_KEYWORD_LEN = 128


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


class ManufacureEnv(Env):
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
        Initialize manufacturing ontology env.

        Args:
            config_manager: Per-Agent ConfigManager; required unless ``ONTOLOGY_URL`` is set in the environment.
        """
        url_env = os.getenv("ONTOLOGY_URL")
        if url_env:
            self.base_url = url_env
        elif config_manager is not None:
            self.base_url = config_manager.get("ONTOLOGY.api.url")
        else:
            raise RuntimeError("ManufacureEnv requires per-Agent config_manager or ONTOLOGY_URL environment variable.")
        super().__init__()

    def init(self):
        pass

    @Env.tool
    def get_ontology_description(self):
        """
        获取数据相关的本体描述。
        """
        url = f"{self.base_url}/get_object_types"
        object_type = requests.get(url).json()["result"]

        url = f"{self.base_url}/get_object_relations"
        object_relations = requests.get(url).json()["result"]

        url = f"{self.base_url}/get_nodes_attr"
        nodes_attr = requests.get(url).json()["result"]

        url = f"{self.base_url}/get_edges_attr"
        edges_attr = requests.get(url).json()["result"]

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
            "frontend_msg": "get_ontology_info_message_Fake",
        }

    @Env.tool
    def get_BusinessProcedure(self, keywords: list[str]):
        """
        使用场景：
            - 根据属性条件定位某一具体业务流程节点

        函数功能：
            - 通过属性过滤查询 BusinessProcedure 节点
            - 若命中唯一节点，返回其完整属性信息

        Args:
            keywords: list[str],可以输入多个关键词（短词）, 其中有一个匹配到对应的业务逻辑则会返回

        Returns:
            业务逻辑节点的属性信息描述
        """
        keywords = _normalize_business_keywords(keywords)
        title_str = "CONTAINS " + " OR CONTAINS ".join([f"'{k}'" for k in keywords])
        url = f"{self.base_url}/property_filter"
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

        property_info_search_url = f"{self.base_url}/property_info_search"
        for cur_info_uuid in result:
            uuid_result = cur_info_uuid["n.uuid"]
            parameters = {
                "element_class": "BusinessProcedure",
                "element_type": "NODE",
                "element_uuid": uuid_result,
            }
            resp = requests.post(property_info_search_url, json=parameters)
            for cur_info in resp.json().get("result", []):
                result_all.append(
                    {
                        "title": cur_info["properties"]["title"],
                        "procedureContent": cur_info["properties"]["procedureContent"],
                    }
                )

        return {
            "original": "查询到相关业务逻辑如下：\n" + _pretty_json_for_display(result_all),
            "frontend_msg": "查询到业务逻辑。",
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
        url = f"{self.base_url}/property_filter"
        parameters = {
            "element_class": object_type,
            "element_type": "NODE",
            "filter_dict": {},
        }
        resp = requests.post(url, json=parameters)
        result = resp.json().get("result", None)
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
        url = f"{self.base_url}/property_info_search"
        parameters = {
            "element_class": object_type,
            "element_type": "NODE",
            "element_uuid": uuid,
        }
        resp = requests.post(url, json=parameters)
        result = resp.json().get("result", None)
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
        url = f"{self.base_url}/property_filter"
        parameters = {
            "element_class": relation_type,
            "element_type": "EDGE",
            "filter_dict": {},
        }
        resp = requests.post(url, json=parameters)
        result = resp.json().get("result", None)
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
        url = f"{self.base_url}/property_info_search"
        parameters = {
            "element_class": relation_type,
            "element_type": "EDGE",
            "element_uuid": uuid,
        }
        resp = requests.post(url, json=parameters)
        result = resp.json().get("result", None)
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
        url = f"{self.base_url}/hop_search"
        parameters = {"uuid": uuid, "hop_num": hop_num, "accurate_flag": accurate_flag}
        resp = requests.post(url, json=parameters)
        result = resp.json().get("result", None)
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
            - 查询两个实体类型之间的所有关系路径(包括起始node，结束node)
        函数功能：
            - 根据起始实体类型、关系类型、方向和结束实体类型进行路径搜索
        Args:
            start_object_type:
                起始节点类型标签
            relation_type:
                关系类型标签
            direction:
                关系方向，取值为 "->"(单向) 或 "-"(双向)
            end_object_type:
                结束节点类型标签
        Returns:
            符合条件的路径及其节点、关系信息
        Examples:
            >>> pattern_search("MPart","Certifies","->","Supplier")
            [
                {
                "a.name": None,
                "a.uuid": "ac949211-ff21-418f-b8dc-6d3200cf54c3",
                "a.id": "B002",
                "c.name": None,
                "c.uuid": "acf6b73e-ef6d-460b-8aa3-189c295db5b1",
                "c.id": "S01"
                }
            ]
        """
        url = f"{self.base_url}/pattern_search"
        path_pattern = [[f"{start_object_type}", {}], [f"{relation_type}", direction], [f"{end_object_type}", {}]]
        parameters = {"path_pattern": path_pattern, "return_vars": ["a", "c"]}
        resp = requests.post(url, json=parameters)
        result = resp.json().get("result", None)
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
        hop_search_url = f"{self.base_url}/hop_search"
        parameters = {"uuid": startNodeUUID, "hop_num": 1, "accurate_flag": True}
        hop_search_result = requests.post(url=hop_search_url, json=parameters).json().get("result", None)
        if not hop_search_result:
            return {"original": "未查到相关边实例。", "frontend_msg": "未查到相关边实例。"}
        result_all = []
        for cur_info in hop_search_result:
            if realtionType == cur_info["relations"][0]["relation_type"]:
                edge_uuid = cur_info["relations"][0]["uuid"]
                property_info_search_url = f"{self.base_url}/property_info_search"
                parameters = {
                    "element_class": cur_info["relations"][0]["relation_type"],
                    "element_type": "EDGE",
                    "element_uuid": edge_uuid,
                }
                property_resp = requests.post(property_info_search_url, json=parameters)
                property_result = property_resp.json().get("result", None)
                if property_result:
                    result_all.append(property_result[0])
        return {
            "original": f"查询成功，以{startNodeUUID}为起点，关系类型为{realtionType}的边信息有：{result_all}",
            "frontend_msg": "查询成功，返回路径实例。",
        }

    @Env.tool
    def get_relation_by_endID(self, endNodeID: str, realtionType: str):
        """
        使用场景：
            - 查询某个节点结束的边
        Args:
            endNodeID(str): 终止节点的UUID(**注意:是uuid**)
            realtionType(str): 关系的类型
        Returns:
            以终止节点UUID为终点，关系类型为realationType的边信息
        """
        hop_search_url = f"{self.base_url}/hop_search"
        parameters = {"uuid": endNodeID, "hop_num": 1, "accurate_flag": True}
        hop_search_result = requests.post(url=hop_search_url, json=parameters).json().get("result", None)
        if not hop_search_result:
            return {"original": "未查到相关边实例。", "frontend_msg": "未查到相关边实例。"}
        result_all = []
        for cur_info in hop_search_result:
            if realtionType == cur_info["relations"][0]["relation_type"]:
                edge_uuid = cur_info["relations"][0]["uuid"]
                property_info_search_url = f"{self.base_url}/property_info_search"
                parameters = {
                    "element_class": cur_info["relations"][0]["relation_type"],
                    "element_type": "EDGE",
                    "element_uuid": edge_uuid,
                }
                property_resp = requests.post(property_info_search_url, json=parameters)
                property_result = property_resp.json().get("result", None)
                if property_result:
                    result_all.append(property_result[0])
        return {
            "original": f"查询成功，以{endNodeID}为终点，关系类型为{realtionType}的边信息有：{result_all}",
            "frontend_msg": "查询成功，返回路径实例。",
        }

    @Env.tool
    def calculate_time_diff(self, start_date: str, end_date: str):
        """
        使用场景：
            - 计算两个日期之间的时间差
        Args:
            start_date (str): 开始日期，格式 "yyyy-mm-dd"
            end_date (str): 结束日期，格式 "yyyy-mm-dd"
        Returns:
            日期差值计算结果
        """
        date_format = "%Y-%m-%d"
        try:
            start_dt = datetime.datetime.strptime(start_date, date_format)
            end_dt = datetime.datetime.strptime(end_date, date_format)
        except ValueError:
            return {"original": "日期格式错误或计算失败。", "frontend_msg": "日期格式错误或计算失败。"}

        delta_days = (end_dt - start_dt).days
        result = {
            "start_date": start_date,
            "end_date": end_date,
            "time_diff_days": delta_days,
            "unit": "days",
        }

        return {
            "original": _pretty_json_for_display(result),
            "frontend_msg": "计算成功，返回时间差.",
        }

    @Env.tool
    def calculate_final_date(self, base_date: str, time_diff: int, diff_unit: str = "days"):
        """
        使用场景：
            - 根据基准绝对时间和时间差值，计算最终的绝对时间
        Args:
            base_date (str): 基准日期，格式 "yyyy-mm-dd"
            time_diff (int): 时间差值（正负均可）
            diff_unit (str): 单位，默认 "days"
        Returns:
            最终绝对时间计算结果
        """
        date_format = "%Y-%m-%d"
        try:
            base_dt = datetime.datetime.strptime(base_date, date_format)
        except ValueError as e:
            return {
                "original": f"日期计算失败：{str(e)}",
                "frontend_msg": "日期格式错误或参数无效，计算失败。",
            }
        supported_units = ["days"]
        if diff_unit not in supported_units:
            raise ValueError(f"不支持的时间单位：{diff_unit}，仅支持 {supported_units}")
        from datetime import timedelta

        if diff_unit == "days":
            final_dt = base_dt + timedelta(days=time_diff)
        result = {
            "base_date": base_date,
            "time_diff": time_diff,
            "diff_unit": diff_unit,
            "final_date": final_dt.strftime(date_format),
            "calculation_logic": f"{base_date} {'+' if time_diff >= 0 else '-'} {abs(time_diff)} {diff_unit}",
        }

        return {
            "original": _pretty_json_for_display(result),
            "frontend_msg": "计算成功，返回最终绝对时间.",
        }
