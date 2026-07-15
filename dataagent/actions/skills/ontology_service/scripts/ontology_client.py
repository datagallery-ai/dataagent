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
import json
import os
from dataclasses import dataclass
from typing import Any

import requests

DEFAULT_TIMEOUT = 120


def parse_maybe_structured(value: Any) -> Any:
    """尝试将值解析为结构化数据（dict/list），失败则返回原值。"""
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, bool, int, float)):
        return value

    text = str(value).strip()
    if not text:
        return None

    try:
        return json.loads(text)
    except Exception:
        return _safe_literal_eval(text)


def _safe_literal_eval(value: Any) -> Any:
    """安全地将字符串解析为 Python 字面量。"""
    if isinstance(value, str):
        try:
            return ast.literal_eval(value)
        except Exception:
            return value
    return value


OPERATORS = ["IS NOT NULL", "IS NULL", "<=", ">=", "=", "<", ">", "CONTAINS", "STARTS WITH", "ENDS WITH", "IN"]


def build_filter_dict(
    equal: dict[str, Any] | None = None,
    not_equal: dict[str, Any] | None = None,
    contains: dict[str, Any] | None = None,
    starts_with: dict[str, Any] | None = None,
    ends_with: dict[str, Any] | None = None,
    gt: dict[str, Any] | None = None,
    gte: dict[str, Any] | None = None,
    lt: dict[str, Any] | None = None,
    lte: dict[str, Any] | None = None,
    is_null: list[str] | None = None,
    is_not_null: list[str] | None = None,
    in_list: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    """Build a filter_dict from structured parameters.

    Args:
        equal: Exact match filters, e.g. {"name": "Alice", "status": "active"}
        not_equal: Not equal filters, e.g. {"status": "inactive"}
        contains: Substring match filters, e.g. {"name": "Alice", "desc": "premium"}
        starts_with: Prefix match filters, e.g. {"code": "ABC"}
        ends_with: Suffix match filters, e.g. {"ext": ".pdf"}
        gt: Greater than filters, e.g. {"age": 18}
        gte: Greater than or equal filters, e.g. {"score": 60}
        lt: Less than filters, e.g. {"price": 100}
        lte: Less than or equal filters, e.g. {"qty": 10}
        is_null: Properties that should be NULL, e.g. ["deleted_at", "remark"]
        is_not_null: Properties that should NOT be NULL, e.g. ["name", "created_at"]
        in_list: IN list filters, e.g. {"status": ["active", "pending"], "type": ["A", "B"]}

    Returns:
        A normalized filter_dict ready for API calls.
    """
    result: dict[str, Any] = {}

    def add_condition(conditions: dict[str, Any], op: str) -> None:
        for prop, val in conditions.items():
            if op == "=":
                result[prop] = f"= '{val}'"
            elif op == "!=":
                result[prop] = f"!= '{val}'"
            elif op == "CONTAINS":
                result[prop] = f"CONTAINS '{val}'"
            elif op == "STARTS WITH":
                result[prop] = f"STARTS WITH '{val}'"
            elif op == "ENDS WITH":
                result[prop] = f"ENDS WITH '{val}'"
            elif op == ">":
                result[prop] = f"> {val}"
            elif op == ">=":
                result[prop] = f">= {val}"
            elif op == "<":
                result[prop] = f"< {val}"
            elif op == "<=":
                result[prop] = f"<= {val}"

    if equal:
        add_condition(equal, "=")
    if not_equal:
        add_condition(not_equal, "!=")
    if contains:
        add_condition(contains, "CONTAINS")
    if starts_with:
        add_condition(starts_with, "STARTS WITH")
    if ends_with:
        add_condition(ends_with, "ENDS WITH")
    if gt:
        add_condition(gt, ">")
    if gte:
        add_condition(gte, ">=")
    if lt:
        add_condition(lt, "<")
    if lte:
        add_condition(lte, "<=")

    if is_null:
        for prop in is_null:
            result[prop] = "IS NULL"

    if is_not_null:
        for prop in is_not_null:
            result[prop] = "IS NOT NULL"

    if in_list:
        for prop, vals in in_list.items():
            items = ", ".join(f"'{v}'" for v in vals)
            result[prop] = f"IN [{items}]"

    return result


def infer_element_type(element_class: str) -> str:
    """根据元素类名推断是节点还是边。边类型通常包含 '-'。"""
    if "-" in element_class:
        return "EDGE"
    return "NODE"


def normalize_filter_dict(filter_dict: Any) -> dict[str, Any]:
    """规范化过滤字典，自动补全缺少的操作符。"""
    parsed = parse_maybe_structured(filter_dict) or {}
    if not isinstance(parsed, dict):
        return {}
    normalized = {}
    for key, value in parsed.items():
        if isinstance(value, str):
            val = value.strip()
            if val and not _has_operator(val):
                normalized[key] = f"= '{val}'"
            else:
                normalized[key] = value
        else:
            normalized[key] = value
    return normalized


def _has_operator(value: str) -> bool:
    """检查值是否已包含操作符。"""
    upper = value.upper()
    return any(op in upper for op in OPERATORS)


def _resolve_url_for_scene(raw: str, scene: str) -> str:
    """Resolve a plain URL or a scene-keyed URL mapping."""
    parsed = parse_maybe_structured(raw)
    if not isinstance(parsed, dict):
        return str(raw or "").rstrip("/")

    for key in (scene, "default", "DEFAULT"):
        if key and key in parsed:
            return str(parsed[key]).rstrip("/")
    if len(parsed) == 1:
        return str(next(iter(parsed.values()))).rstrip("/")
    return ""


class OntologyClientError(RuntimeError):
    pass


@dataclass(slots=True)
class OntologyClient:
    scene: str = ""
    base_url: str = ""
    search_base_url: str = ""
    action_base_url: str = ""
    timeout: int = DEFAULT_TIMEOUT

    @staticmethod
    def _collect_matching_edge_info(hop_result: list[Any], relation_type: str) -> list[Any]:
        """从跳搜索结果中收集匹配指定关系类型的边信息。"""
        matching_edges = []
        for path in hop_result:
            for relation in path.get("relations", []):
                if relation.get("relation_type") == relation_type:
                    matching_edges.append({"e.uuid": relation.get("uuid")})
        return matching_edges

    @classmethod
    def from_env(
        cls,
        *,
        scene: str | None = None,
        ontology_url: str | None = None,
        search_base_url: str | None = None,
        action_base_url: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> OntologyClient:
        """从环境变量或参数创建客户端实例。"""
        resolved_scene = scene if scene is not None else os.getenv("SCENE", "")
        base_raw = ontology_url if ontology_url is not None else os.getenv("ONTOLOGY_URL", "")
        resolved_base = _resolve_url_for_scene(base_raw, resolved_scene)
        resolved_search = search_base_url or os.getenv("ONTOLOGY_SEARCH_URL") or ""
        resolved_action = action_base_url or os.getenv("ONTOLOGY_ACTION_URL") or ""
        if not resolved_search and resolved_base:
            resolved_search = f"{resolved_base}/api/v1/search"
        if not resolved_action and resolved_base:
            resolved_action = f"{resolved_base}/api/v1/action/ontologies/actions/execute"

        return cls(
            scene=resolved_scene,
            base_url=resolved_base,
            search_base_url=resolved_search,
            action_base_url=resolved_action,
            timeout=timeout,
        )

    def append_scene_param(self, url: str) -> str:
        """为 URL 添加 scene_name 查询参数。"""
        scene = self.scene
        if not scene:
            return url
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}scene_name={scene}"

    def search_post(self, api: str, payload: dict[str, Any]) -> dict[str, Any]:
        """向搜索 API 发送 POST 请求。"""
        base = self._ensure_search_base_url().rstrip("/")
        url = f"{base}/{api.lstrip('/')}"
        url = self.append_scene_param(url)
        return self._post_json(url, payload)

    def action_post(self, api: str, payload: dict[str, Any]) -> dict[str, Any]:
        """向动作 API 发送 POST 请求。"""
        base = self._ensure_action_base_url().rstrip("/")
        url = f"{base}/{api.lstrip('/')}" if api else base
        url = self.append_scene_param(url)
        return self._post_json(url, payload)

    def describe_ontology(self) -> dict[str, Any]:
        """获取本体结构描述（类型、关系、属性）。"""
        search_base_url = self._ensure_search_base_url().rstrip("/")
        return {
            "scene": self.scene,
            "search_base_url": search_base_url,
            "object_types": self._get_json(f"{search_base_url}/get_object_types").get("result", []),
            "object_relations": self._get_json(f"{search_base_url}/get_object_relations").get("result", []),
            "nodes_attr": self._get_json(f"{search_base_url}/get_nodes_attr").get("result", []),
            "edges_attr": self._get_json(f"{search_base_url}/get_edges_attr").get("result", []),
        }

    def property_filter(
        self,
        element_class: str,
        element_type: str,
        filter_dict: Any,
        *,
        get_all_properties: bool | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        """根据属性条件过滤节点或边。"""
        payload: dict[str, Any] = {
            "element_class": element_class,
            "element_type": element_type,
            "filter_dict": parse_maybe_structured(filter_dict) or {},
        }
        if get_all_properties is not None:
            payload["get_all_properties"] = get_all_properties
        if limit is not None:
            payload["limit"] = limit
        if offset is not None:
            payload["offset"] = offset
        return self.search_post("property_filter", payload).get("result")

    def property_info_search(self, element_class: str, element_type: str, element_uuid: str) -> Any:
        """查询指定节点或边的完整属性信息。"""
        payload = {
            "element_class": element_class,
            "element_type": element_type,
            "element_uuid": element_uuid,
        }
        return self.search_post("property_info_search", payload).get("result", [])

    def get_object_info(self, object_type: str, limit: int | None = None, offset: int | None = None) -> Any:
        """获取指定类型的所有节点实例。"""
        return self.property_filter(object_type, "NODE", {}, limit=limit, offset=offset)

    def get_node_info(self, object_type: str, uuid: str) -> Any:
        """获取指定节点实例的属性。"""
        return self.property_info_search(object_type, "NODE", uuid)

    def get_relation_info(self, relation_type: str, limit: int | None = None, offset: int | None = None) -> Any:
        """获取指定类型的所有边实例。"""
        return self.property_filter(relation_type, "EDGE", {}, limit=limit, offset=offset)

    def get_edge_info(self, relation_type: str, uuid: str) -> Any:
        """获取指定边实例的属性。"""
        return self.property_info_search(relation_type, "EDGE", uuid)

    def hop_search(
        self,
        uuid: str,
        hop_num: int,
        accurate_flag: bool,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        """执行多跳图搜索，返回从指定节点可达的路径。"""
        payload = {"uuid": uuid, "hop_num": hop_num, "accurate_flag": accurate_flag}
        if limit is not None:
            payload["limit"] = limit
        if offset is not None:
            payload["offset"] = offset
        return self.search_post("hop_search", payload).get("result")

    def pattern_search(
        self,
        start_object_type: str,
        relation_type: str,
        direction: str,
        end_object_type: str,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        """执行起点-关系-终点的图模式搜索。"""
        payload = {
            "path_pattern": [
                [start_object_type, {}],
                [relation_type, direction],
                [end_object_type, {}],
            ],
            "return_vars": ["var0", "var2"],
        }
        if limit is not None:
            payload["limit"] = limit
        if offset is not None:
            payload["offset"] = offset
        return self.search_post("pattern_search", payload).get("result")

    def get_relation_by_start_uuid(self, start_node_uuid: str, relation_type: str) -> list[Any]:
        """从起点节点查找指定类型的所有关系。"""
        hop_result = self.hop_search(start_node_uuid, 1, True) or []
        return self._collect_matching_edge_info(hop_result, relation_type)

    def get_relation_by_end_uuid(self, end_node_uuid: str, relation_type: str) -> list[Any]:
        """从终点节点查找指定类型的所有关系。"""
        hop_result = self.hop_search(end_node_uuid, 1, True) or []
        return self._collect_matching_edge_info(hop_result, relation_type)

    def get_sub_graph(self, instance_id: str, limit: int | None = None) -> Any:
        """获取以节点为中心的两跳邻居子图。"""
        url = f"{self._ensure_search_base_url().rstrip('/')}/entity_details/{instance_id}"
        params = {}
        if self.scene:
            params["scene_name"] = self.scene
        if limit is not None:
            params["limit"] = limit
        response = requests.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        return response.json().get("result")

    def count_search(self, element_class: str, element_type: str, filter_dict: Any) -> Any:
        """统计满足条件的节点或边数量。"""
        payload = {
            "element_class": element_class,
            "element_type": element_type,
            "filter_dict": parse_maybe_structured(filter_dict) or {},
        }
        return self.search_post("count_search", payload).get("result")

    def aggregate_search(
        self,
        element_class: str,
        element_type: str,
        target_property: str,
        agg: str,
        filter_dict: Any,
    ) -> Any:
        """对目标属性执行聚合计算（SUM/AVG/MIN/MAX/COUNT）。"""
        payload = {
            "element_class": element_class,
            "element_type": element_type,
            "target_property": target_property,
            "agg": agg,
            "filter_dict": parse_maybe_structured(filter_dict) or {},
        }
        return self.search_post("aggregate_search", payload).get("result")

    def sorted_search(
        self,
        element_class: str,
        element_type: str,
        filter_dict: Any,
        return_properties: Any,
        sort_by: str | None,
        ascending: bool,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Any:
        """返回按指定属性排序的过滤结果。"""
        payload = {
            "element_class": element_class,
            "element_type": element_type,
            "filter_dict": parse_maybe_structured(filter_dict) or {},
            "return_properties": parse_maybe_structured(return_properties),
            "sort_by": sort_by,
            "ascending": ascending,
        }
        if limit is not None:
            payload["limit"] = limit
        if offset is not None:
            payload["offset"] = offset
        return self.search_post("sorted_search", payload).get("result")

    def list_actions(self) -> list[dict[str, Any]]:
        """列出当前场景下服务端声明的本体动作。"""
        scene_name = self.scene
        base = self._ensure_action_base_url().replace("/execute", "").rstrip("/")
        url = f"{base}/get/scene/{scene_name}"
        response = requests.get(url, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        return data.get("data", {}).get("actions", [])

    def action_to_uuid(self, action_name: str) -> str | None:
        """根据动作名称查找对应的动作 UUID。"""
        for action in self.list_actions():
            if action.get("name") == action_name:
                return action.get("id")
        return None

    def run_action(
        self,
        *,
        action_name: str | None = None,
        action_id: str | None = None,
        instance_type: str = "entity",
        instance_api_name: str | None = None,
        instance_id: str | None = None,
        input_params: Any = None,
    ) -> dict[str, Any]:
        """执行服务端已声明的通用本体动作。"""
        resolved_action_id = action_id
        if not resolved_action_id and action_name:
            resolved_action_id = self.action_to_uuid(action_name)
        if not resolved_action_id:
            raise OntologyClientError("Unable to resolve action id. Pass --action-id or a valid --action-name.")

        payload = {
            "instance_type": instance_type,
            "instance_api_name": instance_api_name,
            "instance_id": instance_id,
            "action_id": resolved_action_id,
            "input_params": parse_maybe_structured(input_params) or {},
        }
        return self.action_post("", payload)

    def _with_scene_name(self, url: str) -> str:
        """为 URL 添加 scene_name 查询参数。"""
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}scene_name={self.scene}" if self.scene else url

    def _ensure_search_base_url(self) -> str:
        """确保搜索 URL 已配置，否则抛出异常。"""
        if not self.search_base_url:
            raise OntologyClientError(
                "Search base URL is missing. Set ONTOLOGY_URL or ONTOLOGY_SEARCH_URL, or pass --search-base-url."
            )
        return self.search_base_url

    def _ensure_action_base_url(self) -> str:
        """确保动作 URL 已配置，否则抛出异常。"""
        if not self.action_base_url:
            raise OntologyClientError(
                "Action base URL is missing. Set ONTOLOGY_URL or ONTOLOGY_ACTION_URL, or pass --action-base-url."
            )
        return self.action_base_url

    def _get_json(self, url: str) -> dict[str, Any]:
        """发送 GET 请求并返回 JSON 响应。"""
        response = requests.get(self._with_scene_name(url), timeout=self.timeout)
        try:
            response.raise_for_status()
        except requests.HTTPError:
            error_detail = response.text
            try:
                error_data = response.json()
                detail = error_data.get("detail", error_detail)
            except Exception:
                detail = error_detail
            raise OntologyClientError(f"HTTP {response.status_code}: {detail}") from None
        return response.json()

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """发送 POST 请求并返回 JSON 响应。"""
        response = requests.post(self._with_scene_name(url), json=payload, timeout=self.timeout)
        try:
            response.raise_for_status()
        except requests.HTTPError:
            error_detail = response.text
            try:
                error_data = response.json()
                detail = error_data.get("detail", error_detail)
            except Exception:
                detail = error_detail
            raise OntologyClientError(f"HTTP {response.status_code}: {detail}") from None
        return response.json()
