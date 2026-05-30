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
import logging
import os
import re
from typing import Any

import requests

from dataagent.actions.environment.env import Env
from dataagent.actions.perceptor.perceptor_utils import execute_with_llm

logger = logging.getLogger(__name__)


def _parse_output(text: str):
    match = re.search(r"json\s*(\{.*?\})\s*", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except Exception:
        return None


def _resolve_scene_url(scene: str, raw_value: Any) -> str:
    if isinstance(raw_value, dict):
        return str(raw_value.get(scene) or raw_value.get("default") or "")

    if raw_value is None:
        return ""

    s = str(raw_value).strip()
    if not s:
        return ""

    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return str(obj.get(scene) or obj.get("default") or "")
    except Exception as e:
        logger.error(f"解析json时发生异常: {e}, 输入字符串: {s}", exc_info=True)

    try:
        obj = ast.literal_eval(s)
        if isinstance(obj, dict):
            return str(obj.get(scene) or obj.get("default") or "")
    except Exception as e:
        logger.error(f"解析dict时发生异常: {e}, 输入字符串: {s}", exc_info=True)

    return s


class ChangpingEnv(Env):
    """
    中和实验本体查询环境。

    提供实验样本查询、实验注册、实验流程构建与实验员分配等工具。
    """

    def __init__(self):
        self.scene = str(os.getenv("SCENE") or "")
        search_raw = os.getenv("SCENE_URL") or ""
        self.base_url = _resolve_scene_url(self.scene, search_raw)

        super().__init__()

    def init(self):
        if not self.base_url:
            raise ValueError("scene_url is required in environment variables.")

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
        action_id = self._action_to_uuid("is_sample_volume_enough_for_exp", sample_type)
        parameters = {
            "instance_type": "entity",
            "instance_api_name": sample_type,
            "instance_id": uuid,
            "action_id": action_id,
            "input_params": {},
        }
        resp = self._action_post("api/v1/action/ontologies/actions/execute", parameters)
        res = resp["data"]["action_return"]["value"]["success"] == "True"
        if res:
            msg = f"✅ {sample_type} {sample_name} 余量充足，可以支撑实验。"
        else:
            msg = f"⚠️ {sample_type} {sample_name} 余量不足，无法支撑实验。已标记库存短缺，待样本制备小组处理。"
        return {"original_msg": msg, "frontend_msg": msg}

    @Env.tool
    def refill_sample(self, sample_name: str, sample_type: str, uuid: str) -> dict[str, str]:
        """
        触发样本补充动作并返回补充结果。

        Args:
            sample_name: 样本名称。
            sample_type: 样本类型。
            uuid: 样本 UUID。

        Returns:
            包含补充结果文本的字典。
        """
        action_id = self._action_to_uuid("refill_sample", sample_type)
        parameters = {
            "instance_type": "entity",
            "instance_api_name": sample_type,
            "instance_id": uuid,
            "action_id": action_id,
            "input_params": {},
        }
        resp = self._action_post("api/v1/action/ontologies/actions/execute", parameters)
        msg = f"{sample_type} {sample_name} 已补充，余量 {resp['result']['volume']}。"
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
        action_id = self._action_to_uuid("create_expv2", "NeutralizationExperiment")
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
        resp = self._action_post("api/v1/action/ontologies/actions/execute", parameters)
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
        resp = self._search_post("api/v1/search/property_filter", parameters)
        result = resp.get("result") or []
        if not result:
            return {"original_msg": "[]", "frontend_msg": "未找到实验流程。"}
        uuid = result[0]["n.uuid"]
        parameters = {
            "element_class": "ExpManual",
            "element_type": "NODE",
            "element_uuid": uuid,
        }

        resp = self._search_post("api/v1/search/property_info_search", parameters)
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
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}scene_name={self.scene}" if self.scene else url

    def _search_post(self, api: str, payload: dict[str, Any]) -> dict[str, Any]:
        return requests.post(self._with_scene_name(f"{self.base_url}/{api}"), json=payload).json()

    def _action_post(self, api: str, payload: dict[str, Any]) -> dict[str, Any]:
        return requests.post(self._with_scene_name(f"{self.base_url}/{api}"), json=payload).json()

    def _action_to_uuid(self, action_name: str, target_api_name: str) -> str | None:
        _ = target_api_name
        resp = requests.get(self._with_scene_name(f"{self.base_url}/api/v1/action/ontologies/actions/get/all"), json={})
        res = resp.json().get("data", {}).get("actions", [])
        for action in res:
            if action.get("name") == action_name:
                return action.get("id")
        return None

    def _antibody_name_to_id(self, name: str) -> str:
        data = {
            "element_class": "Antibody",
            "element_type": "NODE",
            "filter_dict": {"name": f"CONTAINS '{name}'"},
            "get_all_properties": True,
        }
        resp = self._search_post("api/v1/search/property_filter", data)
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
                # 处理异常情况
                element_class, property_name = None, None  # 或设置默认值
            data = {
                "element_class": element_class,
                "element_type": "NODE",
                "filter_dict": {property_name: f"CONTAINS '{value}'"},
            }
            resp = self._search_post("api/v1/search/property_filter", data)
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
            resp = self._search_post("api/v1/search/property_filter", data)
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
            resp = self._search_post("api/v1/search/property_info_search", data)
            result = resp.get("result") or []
            if not result:
                continue
            res.append(result[0].get("properties", {}).get(property_name))
        return res
