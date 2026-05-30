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

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import networkx as nx
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from loguru import logger

from dataagent.core.context.context_trajectory import Context
from dataagent.core.managers.prompt_manager.template import PromptTemplate
from dataagent.utils.constants import DEFAULT_MAX_TOOL_RESULT_LENGTH
from dataagent.utils.parsing_utils import extract_action_payloads, parse_action_payloads_to_tool_calls

MAX_TOOL_RESULT_LENGTH = DEFAULT_MAX_TOOL_RESULT_LENGTH


def write_result_to_workspace(content: str, tool_name: str, workspace: Path) -> Path:
    """将工具结果写入 workspace 的持久化文件，返回文件路径。"""
    output_dir = workspace / ".dataagent" / "tool_outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    filename = f"{tool_name}_{timestamp}.txt"
    filepath = output_dir / filename
    filepath.write_text(content, encoding="utf-8")
    logger.debug(f"Persisted tool result to {filepath} ({len(content)} chars)")
    return filepath


def truncate_tool_result(content: str, max_length: int = MAX_TOOL_RESULT_LENGTH) -> str:
    """截断超长工具结果，附加定向检索提示。"""
    if not content:
        return str(content) if content is not None else ""
    text = str(content)
    if len(text) <= max_length:
        return text
    return (
        text[:max_length]
        + "\n\n"
        + f"...(truncated: showing first {max_length} chars out of {len(text)} chars."
        + " Reason: very large tool outputs are capped before being returned to the model,"
        + " so they do not flood context or degrade reasoning quality."
        + " If the missing content matters, do not request the same full dump again."
        + " Prefer targeted retrieval instead: rerun the underlying command or query"
        + " with command-side filtering so it returns only the specific field, row,"
        + " match, block, or section you need,"
        + " or use `bash` to inspect only the needed portion directly,"
        + " for example with `head`, `tail`, `rg`, `sed`, or `jq`.)"
    )


def _truncate_tool_message_content(message: ToolMessage, max_length: int = MAX_TOOL_RESULT_LENGTH) -> ToolMessage:
    """兜底截断：确保 ToolMessage content 不超长。

    对于已通过 Executor IR 替换的消息（以 ``[IR Summary]`` 开头），跳过截断。
    """
    content = message.content
    if not isinstance(content, str):
        content = str(content)
    if content.startswith("[IR Summary]"):
        return message
    truncated = truncate_tool_result(content, max_length=max_length)
    if truncated is content:
        return message
    return message.model_copy(update={"content": truncated})


def build_system_message(
    prompt_template: PromptTemplate | None,
    prompt_str: str | None = None,
    **kwargs: Any,
) -> SystemMessage:
    """
    Build a system message from a prompt template and a prompt string.
    """
    return SystemMessage(content=_message_base_build(prompt_template, prompt_str, **kwargs))


def build_human_message(
    prompt_template: PromptTemplate | None = None,
    prompt_str: str | None = None,
    **kwargs: Any,
) -> HumanMessage:
    """
    Build a human message from a prompt template and a prompt string.
    """
    return HumanMessage(content=_message_base_build(prompt_template, prompt_str, **kwargs))


def _message_base_build(
    prompt_template: PromptTemplate | None,
    prompt_str: str | None = None,
    **kwargs: Any,
) -> str:
    """
    Build a message from a prompt template and a prompt string.
    """
    prompt = ""
    if prompt_template is not None:
        prompt = prompt_template.apply_prompt_template(**kwargs) if kwargs else prompt_template.content
    if prompt_str is not None:
        prompt += prompt_str
    if prompt == "":
        prompt = "You are a helpful assistant."  # 保底
    return prompt


def build_ai_message(
    content: str,
    **kwargs: Any,
) -> AIMessage:
    """
    Build an AI message from a content string and a keyword arguments.
    """
    return AIMessage(content=content, **kwargs)


def add_result_tag(message: ToolMessage) -> ToolMessage:
    """为发给模型的 ToolMessage 加上 ``<results>`` 包裹（与 Galatea 约定一致）。

    **不修改**入参对象：原先原地改 ``message.content`` 会导致 ``state["messages"]`` 与
    ``messages.json`` 里同一条在「是否已包标签」上被多次 ``build_messages`` 污染，
    且最后一条若从未再经过 ``build_messages`` 会与前面格式不一致。
    """
    content = message.content
    if not isinstance(content, str):
        content = str(content)
    if content.startswith("<results>"):
        return message
    wrapped = f"<results>\n{content}\n</results>"
    return message.model_copy(update={"content": wrapped})


def build_messages(messages: list[AnyMessage], context: Context | None = None) -> list[AnyMessage]:
    """
    Build a list of messages from a list of base messages.

    When *context* is provided, older ToolMessage contents are replaced with
    compact IR summaries produced by the Context trajectory, keeping only the
    most recent turns in full.
    """
    from dataagent.utils.converter.ir_message_consumer import (
        assign_turn_indices,
        should_replace,
        try_replace_with_ir,
    )

    new_messages = []
    if len(messages) == 0:
        return new_messages

    turn_indices = assign_turn_indices(messages)
    max_turn = max(turn_indices) if turn_indices else 0

    for i, message in enumerate(messages):
        if isinstance(message, AIMessage):
            new_messages.append(message)
        if isinstance(message, ToolMessage):
            if context is not None and should_replace(turn_indices[i], max_turn):
                message = try_replace_with_ir(message, context)
            message = _truncate_tool_message_content(message)
            new_messages.append(add_result_tag(message))
        if isinstance(message, HumanMessage):
            new_messages.append(message)
    return new_messages


def _get_scenario_instruction_from_initial_pt(trajectory: nx.DiGraph, initial_pt: str | None) -> list[str] | None:
    """
    Find a Knowledge node whose predecessor is initial_pt, and return its content.
    If no such node exists, return None.

    Args:
        trajectory: DiGraph trajectory.
        initial_pt: The initial point node id (e.g. the Query node id).

    Returns:
        The knowledge_content list from Knowledge nodes if found, else None.
    """
    if initial_pt is None:
        return None
    instructionlist = []
    for node_id in trajectory.nodes():
        node_attr = trajectory.nodes[node_id]
        if node_attr.get("node_type") != "Knowledge":
            continue
        preds = set(trajectory.predecessors(node_id))
        if preds == {initial_pt}:
            instructionlist.append(node_attr.get("knowledge_content"))
    if len(instructionlist) > 0:
        return instructionlist
    return None


def parse_actions_to_ai_message(text: str) -> AIMessage:
    """
    将一段输出文本中的 `<action>...</action>` 段落转换成 `AIMessage.tool_calls`。

    支持两种 `<action>` 载荷格式：
    1) 新格式（与 `planner/system.md` 一致）：每个 `<action>` 内是单个 JSON 对象
        <action>
        {
            "action_id": 1,
            "description": "...",
            "action_name": "...",
            "action_parameters": {...}
        }
        </action>
    2) 兼容日志常见写法：多行 key=value（可能同时含 action_id/description）
        <action>
        action_name = pdf_extractor
        action_id = 1
        description = 提取pdf文件的内容
        action_parameters = { ... }   # 允许跨多行
        </action>

    - **tool_calls**：标准 LangChain 顶层结构 `[{ "id": "...", "name": "...", "args": {...} }]`
    - **content**：按调用方需求保留原始文本（当前实现不再剥离 `<tasks>/<action>`）
    - **invalid_tool_calls**：对解析失败/字段缺失的 action 生成错误项，供 Executor 输出错误并生成 ToolMessage
    """

    raw = str(text or "")

    payloads = extract_action_payloads(raw)
    tool_calls, invalid_tool_calls, tool_call_meta = parse_action_payloads_to_tool_calls(payloads=payloads)

    # 若存在 </task>，content 只保留到 </task>（含）为止
    m_task_end = re.search(r"</tasks>", raw, flags=re.IGNORECASE)
    content_raw = raw[: m_task_end.end()] if m_task_end else raw
    content = content_raw.strip()
    additional_kwargs = {"_action_meta": tool_call_meta} if tool_call_meta else {}
    return AIMessage(
        content=content,
        tool_calls=tool_calls,
        invalid_tool_calls=invalid_tool_calls,
        additional_kwargs=additional_kwargs,
    )


def record_message(context: Context, message: BaseMessage):
    """
    When a message is produced, it will be processed to a node in the context.
    An AIMessage produced in a planner node will be processed to a StateNode,
        and its tool_call information will be processed to ActionNode (Pending).
    After the action is done, information of the ActionNode will be complemented (output, success).
    """
    if isinstance(message, AIMessage):
        adding_new_pt = True
        if context.messages.get("pending_branch", "Not recorded current pt") == "Not recorded current pt":
            node_kwargs = {
                "node_type": "State",
                "description": "",
                "predecessor_node": _last_node_name(context),  # Only sequential running is supported now
                "state": str(message.content),
            }
            pending_branch = [context.register_node(**node_kwargs)]
            adding_new_pt = False
        else:  # Branch trimmed, need add new branch pointer to pending branch.
            pending_branch = context.messages.pop("pending_branch")

        all_tool_calls = message.tool_calls + message.invalid_tool_calls
        for tool_call in all_tool_calls:
            new_action_node = {
                "label": tool_call["id"],
                "node_type": "Action",
                "description": "",
                "predecessor_node": list(pending_branch),
                "action": tool_call["name"],
                "params": tool_call["args"],
                "output": "Pending",
                "success": False,
                "add_pt": adding_new_pt,
            }
            context.register_node(**new_action_node)
            adding_new_pt = True
    elif isinstance(message, ToolMessage):
        tool_label = f"Action({message.tool_call_id})"
        try:
            context.modify_node(tool_label, {"output": message.content, "success": message.status == "success"})
        except Exception as e:
            logger.error(f"Failed to modify node {tool_label}: {e}")
    else:
        raise ValueError(f"Unsupported message type: {type(message)}")


def _last_node_name(context: Context) -> list[str]:
    """
    Get the name of the last node in the context.
    """
    concurrent_actions = list(context.get_active_branch())
    if not concurrent_actions:
        raise ValueError("No active branch found in context.")
    if len(concurrent_actions) == 1:
        return [concurrent_actions[0]]
    trajectory = context.get_trajectory(trimmed=True)
    predecessor_nodes = []
    for node_name in concurrent_actions:
        predecessor_nodes_of_action = list(trajectory.predecessors(node_name))
        if len(predecessor_nodes_of_action) != 1:
            raise ValueError("当前State的前继Action有多个前继节点，暂且不支持并行搜索")
        predecessor_nodes.append(predecessor_nodes_of_action[0])
    if len(set(predecessor_nodes)) != 1:
        raise ValueError("当前context中存在多个action，且这些action的前继节点不一致，暂且不支持并行搜索")
    return concurrent_actions


def dump_prompt_to_file(
    messages: list[BaseMessage],
    file_path: Path,
    *,
    append: bool = False,
) -> Path:
    """
    将 prepare_prompt 返回的消息列表以可读格式写入文件。

    Args:
        messages: prepare_prompt 返回的 list[BaseMessage]。
        file_path: 输出文件路径，默认 "prompt_dump.txt"。
        append: 为 True 时追加写入，否则覆盖。

    Returns:
        写入的文件路径。
    """
    _MESSAGE_TYPE_LABELS = {
        SystemMessage: "SYSTEM",
        HumanMessage: "HUMAN",
        AIMessage: "AI",
        ToolMessage: "TOOL",
    }
    separator = "=" * 80

    mode = "a" if append else "w"
    file_path.parent.mkdir(parents=True, exist_ok=True)

    with file_path.open(mode, encoding="utf-8") as f:
        f.write(f"{separator}\n")
        f.write(f"  Prompt Dump  |  {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')}  |  {len(messages)} messages\n")
        f.write(f"{separator}\n\n")

        for idx, msg in enumerate(messages):
            label = _MESSAGE_TYPE_LABELS.get(type(msg), type(msg).__name__.upper())
            f.write(f"--- [{idx}] {label} ---\n")

            if isinstance(msg, ToolMessage):
                f.write(f"  tool_call_id: {msg.tool_call_id}\n")
                f.write(f"  status      : {getattr(msg, 'status', 'N/A')}\n")

            content = msg.content
            if isinstance(content, str):
                f.write(f"{content}\n")
            else:
                f.write(f"{json.dumps(content, ensure_ascii=False, indent=2)}\n")

            if isinstance(msg, AIMessage) and msg.tool_calls:
                f.write(f"\n  tool_calls ({len(msg.tool_calls)}):\n")
                for tc in msg.tool_calls:
                    f.write(f"    - id  : {tc['id']}\n")
                    f.write(f"      name: {tc['name']}\n")
                    f.write(f"      args: {json.dumps(tc['args'], ensure_ascii=False, indent=8)}\n")

            if isinstance(msg, AIMessage) and msg.invalid_tool_calls:
                f.write(f"\n  invalid_tool_calls ({len(msg.invalid_tool_calls)}):\n")
                for itc in msg.invalid_tool_calls:
                    f.write(f"    - id   : {itc.get('id')}\n")
                    f.write(f"      name : {itc.get('name')}\n")
                    f.write(f"      error: {itc.get('error')}\n")

            f.write("\n")

        f.write(f"{separator}\n")
        f.write("  END OF DUMP\n")
        f.write(f"{separator}\n")

    logger.debug(f"Prompt dumped to {file_path}")
    return file_path
