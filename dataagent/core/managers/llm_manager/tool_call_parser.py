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
统一的工具调用解析器（仅支持 structured 模式）。

从 ChatNoFunctionCallModel 中提取并扩展，支持 JSON 格式的 tool call 解析。
适用于 structured 模式下，将模型返回的 JSON 文本解析为 tool_calls 列表。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

# 预编译正则表达式，提升性能（用于兜底的 loose JSON 提取）
_LOOSE_JSON_PATTERN = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)


def parse_tool_calls(content: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """
    解析模型返回的文本内容，提取工具调用，并返回清理后的纯推理文本。

    支持的格式（按优先级）：
    1. 纯 JSON 对象/数组（structured 模式标准格式）
    2. 兜底：宽松的 JSON 提取（容错）

    Args:
        content: 模型返回的文本内容。

    Returns:
        (tool_calls, invalid_tool_calls, cleaned_content) 三元组：
        - tool_calls: 成功解析的工具调用列表，每项格式为 {"id": str, "name": str, "args": dict}
        - invalid_tool_calls: 解析失败的工具调用列表，每项格式为 {"id": str, "name": str, "args": str, "error": str}
        - cleaned_content: 清理后的纯推理文本（剥离了工具调用 JSON）
    """
    if not content or not content.strip():
        return [], [], ""

    # 1. 尝试解析纯 JSON 对象（structured 模式标准格式）
    tc, invalid = _parse_json_object(content)
    if tc or invalid:
        # structured 模式：整个 content 都是 JSON，提取 content 字段为推理文本
        cleaned = _extract_content_from_json(content)
        return tc, invalid, cleaned

    # 1.5 特殊情况：整个 content 是合法 JSON 但无 tool_calls（如 name=null）
    # 这是 structured 模式下模型表示"不需要工具调用"的标准响应，
    # 需要从 JSON 中提取 content 字段作为回答文本
    if _is_json_object(content):
        cleaned = _extract_content_from_json(content)
        return [], [], cleaned

    # 2. 兜底：宽松的 JSON 提取（容错方案）
    tc, invalid = _parse_loose_json(content)
    if tc or invalid:
        # 兜底模式：保留原始 content（混合文本难以精确清理）
        return tc, invalid, content

    # 没有找到任何工具调用
    return [], [], content


def _parse_json_object(content: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """尝试将整个内容解析为 JSON 对象或数组。"""
    content_stripped = content.strip()
    if not content_stripped:
        return [], []

    try:
        data = json.loads(content_stripped)
    except json.JSONDecodeError:
        return [], []

    # 单个工具调用：{"name": "...", "arguments": {...}}
    if isinstance(data, dict):
        tc = _extract_tool_call_from_dict(data)
        if tc is not None:
            return [tc], []
        # 如果是 null name，返回空
        if data.get("name") is None:
            return [], []
        # 格式不对，返回 invalid
        return [], [_make_invalid_tool_call(data.get("name", ""), content_stripped, "Invalid JSON structure")]

    # 多个工具调用：[{"name": "...", "arguments": {...}}, ...]
    if isinstance(data, list):
        tool_calls = []
        invalid_tool_calls = []
        for item in data:
            if not isinstance(item, dict):
                continue
            tc = _extract_tool_call_from_dict(item)
            if tc is not None:
                tool_calls.append(tc)
            else:
                invalid_tool_calls.append(
                    _make_invalid_tool_call(
                        item.get("name", ""), json.dumps(item, ensure_ascii=False), "Invalid JSON structure"
                    )
                )
        return tool_calls, invalid_tool_calls

    return [], []


def _parse_loose_json(content: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    兜底方案：宽松的 JSON 提取。

    在文本中查找所有看起来像 JSON 对象的片段 {...}，尝试解析为工具调用。
    这是一个容错方案，用于处理格式不完全符合标准但仍然可解析的情况。

    适用场景：
    - 模型输出了 JSON 但没有按标准格式包装
    - 混合文本 + JSON 的情况
    """
    tool_calls = []
    invalid_tool_calls = []

    # 使用预编译正则查找 JSON 对象（支持一层嵌套）
    matches = _LOOSE_JSON_PATTERN.finditer(content)

    for match in matches:
        json_str = match.group(0)
        try:
            data = json.loads(json_str)
            if not isinstance(data, dict):
                continue

            # 检查是否包含工具调用的关键字段
            if "name" in data:
                # name 为 null 表示不需要调用工具，直接跳过
                if data.get("name") is None:
                    continue

                tc = _extract_tool_call_from_dict(data)
                if tc is not None:
                    tool_calls.append(tc)
                else:
                    # name 存在但格式不对
                    invalid_tool_calls.append(
                        _make_invalid_tool_call(data.get("name", ""), json_str, "Invalid tool call structure")
                    )
        except json.JSONDecodeError:
            # 不是合法 JSON，跳过
            continue

    return tool_calls, invalid_tool_calls


def _is_json_object(content: str) -> bool:
    """检查整个 content 是否为合法的 JSON 对象。"""
    try:
        data = json.loads(content.strip())
        return isinstance(data, (dict, list))
    except json.JSONDecodeError:
        return False


def _extract_content_from_json(content: str) -> str:
    """
    从 structured 模式的 JSON 响应中提取推理文本。

    structured 模式下，整个 content 都是 JSON。如果 JSON 中包含 "content" 字段：
    - 若为工具调用（有 name 且 name 不为 null），content 属于工具调用的一部分，不提取
    - 若非工具调用（如 {"name": null, "content": "回答"}），提取 content 作为推理文本

    Args:
        content: 模型返回的原始 JSON 文本。

    Returns:
        提取到的推理文本，或空字符串。
    """
    try:
        data = json.loads(content.strip())
    except json.JSONDecodeError:
        return ""

    if isinstance(data, dict):
        # 检查是否为工具调用（有 name 字段且 name 不为 null）
        name = data.get("name")
        if name is not None and name != "":
            # 这是工具调用，content 属于工具调用的一部分，不应提取到 cleaned_content
            return ""
        # 非工具调用（如 {"name": null, "content": "..."}），提取 content 字段
        return str(data.get("content", ""))

    if isinstance(data, list):
        # 多工具调用：无推理文本
        return ""

    return ""


def _extract_tool_call_from_dict(data: dict[str, Any]) -> dict[str, Any] | None:
    """
    从字典中提取工具调用，支持多种字段名变体。

    Args:
        data: 包含工具调用信息的字典

    Returns:
        工具调用对象，格式为 {"id": str, "name": str, "args": dict}
        如果无法解析则返回 None
    """
    name = data.get("name")
    if name is None or name == "":
        return None

    # 支持 arguments / args 字段
    args = data.get("arguments") or data.get("args") or {}

    # 如果 args 是字符串，尝试解析为 JSON
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            return None

    return {
        "id": data.get("id") or str(uuid.uuid4()),
        "name": name,
        "args": args if isinstance(args, dict) else {},
    }


def _make_invalid_tool_call(name: str, args: str, error: str) -> dict[str, Any]:
    """创建一个 invalid tool call 记录。"""
    return {"id": str(uuid.uuid4()), "name": name, "args": args, "error": error}
