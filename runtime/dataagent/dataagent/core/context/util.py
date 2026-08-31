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
"""
Context 工具函数（纯函数）。

用于比较 current_actions 与 reasoning_plan 的一致性，以及工具 schema 转换等。
"""

from __future__ import annotations

from typing import Any


def extract_action_name(x: Any) -> str:
    """
    从多种形态的 action 表达中抽取 name：
    - str: 直接 strip
    - dict: 优先取 tool_name / action_name / name
    - object: 优先取 .tool_name / .action_name / .name
    """
    if isinstance(x, str):
        return x.strip()
    if isinstance(x, dict):
        for k in ("tool_name", "action_name", "name"):
            v = x.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    v = getattr(x, "tool_name", None) or getattr(x, "action_name", None) or getattr(x, "name", None)
    return v.strip() if isinstance(v, str) else ""


def is_name_only_payload(d: dict[str, Any]) -> bool:
    """
    判断 dict 是否"只传了 name"（或同义字段）：
    - 除 tool_name/action_name/name 外，其它字段都为空值（None/""/[]/{}/()）则返回 True
    """
    for k, v in d.items():
        if k in ("tool_name", "action_name", "name"):
            continue
        if v not in (None, "", [], {}, ()):
            return False
    return True


def match_current_action_to_expected(curr: Any, expected: dict[str, Any]) -> bool:
    """
    判断 curr 是否与 expected 匹配：
    - 先比 name（必须相同）
    - 若 curr 是 dict 且不止 name，则要求 curr 提供的其它字段与 expected 一致（子集匹配）
    """
    exp_name = extract_action_name(expected)
    cur_name = extract_action_name(curr)
    if not exp_name or exp_name != cur_name:
        return False

    if isinstance(curr, dict) and not is_name_only_payload(curr):
        for k, v in curr.items():
            if k in ("tool_name", "action_name", "name"):
                continue
            if expected.get(k) != v:
                return False
    return True


def actions_match_reasoning_plan(current_actions: Any, plan: Any) -> bool:
    """
    校验 current_actions 与 plan 是否一致：
    - 类型都必须是 list
    - 长度必须相同
    - 逐项 match_current_action_to_expected
    """
    if not (isinstance(current_actions, list) and isinstance(plan, list)):
        return False
    if len(current_actions) != len(plan):
        return False
    for c, e in zip(current_actions, plan, strict=True):
        if not isinstance(e, dict):
            return False
        if not match_current_action_to_expected(c, e):
            return False
    return True


def ToolSchema2Actionschema(tool_schema) -> dict[str, Any]:
    """
    将 tool_schema 转换为 action_schema：
    - key: tool_name
    - value: tool 的各字段信息（description/parameters/output 等）
    """
    from dataagent.core.managers.action_manager import ParameterSchema, ToolSchema

    if not isinstance(tool_schema, ToolSchema):
        raise ValueError("tool_schema must be a ToolSchema")
    parameters: list[ParameterSchema] = tool_schema.parameters
    action_schema = {
        "tool_name": tool_schema.name,
        "tool_description": tool_schema.description,
        "parameters": {p.name: p.type for p in parameters},
        "parameter_descriptions": {p.name: p.description for p in parameters},
        "output": "None",
    }
    return action_schema
