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
Flex current_context formatting utilities.

This module contains pure helper functions that were previously nested inside
`FlexStateExtractor.extract()`. The goal is to reduce complexity in the extractor
while keeping the output behavior unchanged.
"""

from __future__ import annotations

import json
from typing import Any


def safe_json(obj: Any) -> str:
    """尽力把对象序列化为 JSON 字符串；失败时回退为 `str(obj)`（用于展示/日志）。"""
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(obj)


def _format_simple(role: str, content: str, tool_name: str | None = None) -> str:
    """
    Format simple message types with a unified convention.
    Keeps output identical to the previous implementation.
    """
    if role == "Tool":
        return f"<results>\n[Tool:{tool_name or 'tool'}]\n{content} \n</results>"
    return f"[{role}]\n{content}"


def _format_ai_block(content: str, tool_calls: Any, invalid_tool_calls: Any) -> str:
    """
    Format AIMessage content + tool_calls blocks.
    Keeps output identical to the previous implementation.
    """
    parts: list[str] = []
    if content:
        parts.append(f"[Assistant]\n{content}")
    if tool_calls:
        parts.append("[Assistant:tool_calls]\n" + safe_json(compact_tool_calls(tool_calls)))
    if invalid_tool_calls:
        parts.append("[Assistant:invalid_tool_calls]\n" + safe_json(compact_invalid_tool_calls(invalid_tool_calls)))
    return "\n".join(parts).strip() or "[Assistant]\n(Empty)"


def compact_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    """
    将 tool_calls 规整成更紧凑的结构：
    - 保留 name + args
    - 丢弃 id/type 等冗余字段（不会影响“做过什么”的可读性）
    注意：这不是截断，不丢历史，只是更紧凑的展示。
    """
    res: list[dict[str, Any]] = []
    if not tool_calls:
        return res
    if not isinstance(tool_calls, list):
        tool_calls = [tool_calls]
    for tc in tool_calls:
        if isinstance(tc, dict):
            name = tc.get("name") or tc.get("tool") or "tool"
            raw_args = tc.get("args", None)
            if raw_args is None:
                raw_args = tc.get("arguments", {})
            args = raw_args
            res.append({"name": name, "args": args})
        else:
            # 运行时对象：尽量取 name/args
            name = getattr(tc, "name", None) or getattr(tc, "tool", None) or "tool"
            args = getattr(tc, "args", None) or getattr(tc, "arguments", None) or {}
            res.append({"name": str(name), "args": args})
    return res


def compact_invalid_tool_calls(invalid_tool_calls: Any) -> list[dict[str, Any]]:
    """
    将 invalid_tool_calls 规整成更紧凑的结构：
    - 保留 name + error
    - 丢弃其它冗余字段（用于 current_context 展示）
    """
    res: list[dict[str, Any]] = []
    if not invalid_tool_calls:
        return res
    if not isinstance(invalid_tool_calls, list):
        invalid_tool_calls = [invalid_tool_calls]
    for itc in invalid_tool_calls:
        if isinstance(itc, dict):
            res.append(
                {
                    "name": itc.get("name", "tool"),
                    "error": itc.get("error", ""),
                }
            )
        else:
            res.append(
                {
                    "name": str(getattr(itc, "name", "tool")),
                    "error": str(getattr(itc, "error", "")),
                }
            )
    return res


def format_one_message(msg: Any) -> str:
    """
    将单条 message 格式化为一行（或多行）可读文本。
    """
    # 1) 运行时：LangChain BaseMessage 派生类
    if hasattr(msg, "__class__") and hasattr(msg, "content"):
        cls_name = msg.__class__.__name__
        content = getattr(msg, "content", "") or ""

        # AIMessage：可能包含 tool_calls / invalid_tool_calls
        tool_calls = getattr(msg, "tool_calls", None) or []
        invalid_tool_calls = getattr(msg, "invalid_tool_calls", None) or []

        if cls_name == "SystemMessage":
            return _format_simple("System", content)
        if cls_name == "HumanMessage":
            return _format_simple("User", content)
        if cls_name == "ToolMessage":
            tool_name = getattr(msg, "name", "") or "tool"
            return _format_simple("Tool", content, tool_name=str(tool_name))
        if cls_name == "AIMessage":
            return _format_ai_block(content=content, tool_calls=tool_calls, invalid_tool_calls=invalid_tool_calls)
        # 其它未知类型：保底
        return f"[{cls_name}]\n{content}"

    # 2) 持久化/示例：dict 形态（例如 flexstate.json）
    if isinstance(msg, dict):
        msg_type = msg.get("type", "Message")
        content = str(msg.get("content", "") or "")
        name = msg.get("name") or msg.get("additional_kwargs", {}).get("name")

        # tool_calls 有时在 additional_kwargs，有时在顶层（取并集）
        additional_kwargs = msg.get("additional_kwargs", {}) or {}
        tool_calls = msg.get("tool_calls") or additional_kwargs.get("tool_calls") or []
        invalid_tool_calls = additional_kwargs.get("invalid_tool_calls") or []

        if msg_type == "SystemMessage":
            return _format_simple("System", content)
        if msg_type == "HumanMessage":
            return _format_simple("User", content)
        if msg_type == "ToolMessage":
            tool_name = name or "tool"
            return _format_simple("Tool", content, tool_name=str(tool_name))
        if msg_type == "AIMessage":
            return _format_ai_block(content=content, tool_calls=tool_calls, invalid_tool_calls=invalid_tool_calls)
        return f"[{msg_type}]\n{content}"

    # 3) 其它未知类型
    return str(msg)


def build_current_context(messages: Any) -> str:
    """
    将 messages 格式化为 Flex 使用的 current_context 字符串。

    约定：
    - messages[0] 往往是初始 user_query，因此展示时会跳过它（从 messages[1:] 开始）。
    - 若 messages 为空，返回 "None" 以兼容旧行为。
    """
    if not messages:
        return "None"
    blocks = [format_one_message(m) for m in messages[1:]]  # Ignore the initial user query
    return "=== CURRENT CONTEXT ===\n" + "\n\n---\n\n".join(blocks)
