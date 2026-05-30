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
后端无关的工具描述 Prompt 构建。

负责将工具列表转换为提示词文本，供 structured 模式注入到 system message。
langgraph 和 openjiuwen 后端均可复用。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from loguru import logger


def convert_tools_to_openai_schema(tools: list[Any]) -> list[dict[str, Any]]:
    """
    将工具列表统一转为 OpenAI function tools schema。

    支持输入类型：
    - langchain BaseTool / StructuredTool
    - 已经是 OpenAI schema 的 dict
    - 带 name/description/args_schema 的任意对象
    """
    result: list[dict[str, Any]] = []
    for tool in tools:
        # 已经是 OpenAI schema
        if isinstance(tool, dict) and ("function" in tool or tool.get("type") == "function"):
            result.append(tool)
            continue

        # 尝试用 langchain 的转换函数
        try:
            result.append(convert_to_openai_tool(tool))
            continue
        except Exception as e:
            logger.debug(f"convert_to_openai_tool failed for tool '{tool}', falling back to manual conversion: {e}")

        # 手动转换
        name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
        if not name:
            continue
        desc = getattr(tool, "description", None) or ""
        params: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        args_schema = getattr(tool, "args_schema", None)
        if args_schema is not None:
            # 优先 Pydantic v2，fallback 到 v1
            method = getattr(args_schema, "model_json_schema", None) or getattr(args_schema, "schema", None)
            if method:
                params = method()
        result.append(
            {"type": "function", "function": {"name": str(name), "description": str(desc or ""), "parameters": params}}
        )
    return result


def build_tool_calling_prompt(tools_schema: list[dict[str, Any]]) -> str:
    """
    构建 structured 模式的工具调用 prompt。

    生成工具描述 + JSON 响应格式要求，与 response_format=json_object 配合使用，
    强制模型输出合法 JSON。

    Args:
        tools_schema: OpenAI function tools schema 列表。

    Returns:
        注入到 system message 的提示词文本。
    """
    # 构建工具列表
    tool_names = [t.get("function", {}).get("name", "") for t in tools_schema]
    tool_descs = []
    for t in tools_schema:
        func = t.get("function", {})
        tool_descs.append(
            f"- {func.get('name', '')}: {func.get('description', '')}\n"
            f"  Parameters: {json.dumps(func.get('parameters', {}), ensure_ascii=False)}"
        )

    tools_section = (
        "\n\n## Available Tools\n"
        f"You have access to these tools: {', '.join(tool_names)}\n\n" + "\n".join(tool_descs) + "\n"
    )

    # 构建响应格式说明
    return (
        tools_section + "\n## Response Format\n"
        "You MUST respond with a JSON object in one of these formats:\n"
        '- To call a tool: {"name": "tool_name", "arguments": {"param": "value"}, "content": "reason for calling"}\n'
        '- To call multiple tools: [{"name": "tool1", "arguments": {...}, "content": "reason"}, ...]\n'
        '- If no tool call is needed, respond with: {"name": null, "content": "your response"}\n'
        '\nNote: The "content" field is optional but recommended for explaining your reasoning.\n'
    )


def prepend_to_system_message(messages: Any, injection: str) -> Any:
    """
    将文本追加到消息列表的 system message 末尾。

    如果消息列表中已有 system message，则将 injection 追加到其内容末尾；
    如果没有 system message，则在列表头部插入一条新的 system message。

    支持两种消息格式：
    - OpenAI dict messages: [{"role": "system", "content": "..."}, ...]
    - langchain messages: [SystemMessage(content="..."), ...]

    Args:
        messages: 消息列表。
        injection: 要追加的文本。

    Returns:
        修改后的消息列表（不修改原列表）。
    """
    if not isinstance(messages, list) or not messages:
        return messages

    first = messages[0]

    # OpenAI dict 格式
    if isinstance(first, dict) and first.get("role") == "system":
        messages = list(messages)
        messages[0] = {**first, "content": first.get("content", "") + injection}
        return messages

    if isinstance(first, dict) and first.get("role") != "system":
        return [{"role": "system", "content": injection.strip()}] + list(messages)

    # langchain messages 格式
    if isinstance(first, SystemMessage):
        messages = list(messages)
        messages[0] = SystemMessage(content=str(first.content) + injection)
        return messages

    return [SystemMessage(content=injection.strip())] + list(messages)


def inject_after_first_section(messages: Any, injection: str) -> Any:
    """
    将文本插入到 system message 第一个段落/章节之后，而非末尾。

    在长 system prompt 中，追加到末尾的内容容易被 LLM 忽略（lost-in-the-middle）。
    此函数将 injection 插入到首个 ``\\n# `` 标题边界之前，使其紧跟 Role 描述段，
    提高 LLM 对注入内容的关注度。若未找到章节边界，回退到末尾追加。

    Args:
        messages: 消息列表（OpenAI dict 或 langchain 格式）。
        injection: 要插入的文本。

    Returns:
        修改后的消息列表（不修改原列表）。
    """
    if not isinstance(messages, list) or not messages:
        return messages

    first = messages[0]

    def _insert(content: str) -> str:
        boundary = content.find("\n# ", 1)
        if boundary != -1:
            return content[:boundary] + "\n" + injection + content[boundary:]
        return content + injection

    if isinstance(first, dict) and first.get("role") == "system":
        messages = list(messages)
        messages[0] = {**first, "content": _insert(first.get("content", ""))}
        return messages

    if isinstance(first, dict) and first.get("role") != "system":
        return [{"role": "system", "content": injection.strip()}] + list(messages)

    if isinstance(first, SystemMessage):
        messages = list(messages)
        messages[0] = SystemMessage(content=_insert(str(first.content)))
        return messages

    return [SystemMessage(content=injection.strip())] + list(messages)
