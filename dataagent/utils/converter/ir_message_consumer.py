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
IRMessageConsumer — 在 planner 端消费 IR 节点，用 IR 摘要替换较旧的 ToolMessage.content。

与 ResultIRConverter（IR 生产端）对称，本模块是 IR 消费端：
  - 按 turn（AIMessage + 后续 ToolMessages）分组
  - 最近 RECENT_TURNS 轮保留完整 ToolMessage.content
  - 更早的轮次用 IR 摘要替换，节省上下文窗口
  - 若 IR 不存在则 graceful fallback 到原始内容
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from loguru import logger

from dataagent.utils.constants import (
    DEFAULT_IR_KNOWLEDGE_MAX_LEN,
    DEFAULT_IR_RECENT_TURNS,
    DEFAULT_IR_SCRIPT_MAX_LEN,
)

if TYPE_CHECKING:
    from dataagent.core.context.context_trajectory import Context
    from dataagent.core.context.contextIR import DataNode


@dataclass
class DataNodeRenderSnapshot:
    node_type: str
    label: str
    desc: str


def render_ir_summary(data_nodes: list[DataNode], tool_name: str) -> str:
    """将 DataNode 列表渲染为紧凑的文本摘要。

    Args:
        data_nodes: 工具调用产出的数据 IR 节点列表
        tool_name: 工具名称（用于摘要标题）

    Returns:
        可读的 IR 摘要文本
    """
    lines: list[str] = [f"[IR Summary] tool={tool_name}", "Artifacts produced:"]

    for node in data_nodes:
        line = _render_single_node(node)
        if line:
            lines.append(f"- {line}")

    if len(lines) == 2:
        lines.append("- (no artifacts)")

    return "\n".join(lines)


def render_data_node_snapshot(snapshot: DataNodeRenderSnapshot) -> str:
    """Render a data node snapshot with lightweight fields."""
    parts = [f"{snapshot.node_type}({snapshot.label})"]
    if snapshot.desc:
        parts.append(f'"{snapshot.desc}"')
    return " ".join(parts)


def _build_original_content_hint(node: DataNode) -> str | None:
    """根据节点类型生成原始内容的存储位置和恢复方式说明。

    Returns:
        恢复指引字符串，若无可用内容则返回 None
    """
    node_class_name = node.__class__.__name__
    node_type = node_class_name.replace("Node", "") if node_class_name.endswith("Node") else node_class_name

    if node_type == "Table":
        path = getattr(node, "path", None)
        if path:
            return f"Original content: table data stored at `{path}` | To restore: `cat {path}`"
        return "Original content: in-memory table (not persisted)"

    if node_type == "Column":
        from_table = getattr(node, "from_table", None)
        label = getattr(node, "label", "")
        hint = f"Original content: column data from table `{from_table}`"
        if label:
            hint += f", column `{label}`"
        return hint

    if node_type == "Script":
        path = getattr(node, "path", None)
        script_content = getattr(node, "script_content", None)
        if path:
            return f"Original content: script stored at `{path}` | To restore: `cat {path}`"
        if script_content:
            truncated = script_content[:DEFAULT_IR_SCRIPT_MAX_LEN]
            if len(script_content) > DEFAULT_IR_SCRIPT_MAX_LEN:
                truncated += "\n... [truncated]"
            return f"Original content: inline script\n```\n{truncated}\n```"
        return None

    if node_type == "File":
        path = getattr(node, "path", None)
        if path:
            return (
                f"Original content: file stored at `{path}` | "
                f'To inspect: use `read_file` with path="{path}" and appropriate offset/limit, '
                f"or use `grep` with pattern matching to search for relevant content"
            )
        return None

    if node_type == "Skill":
        path = getattr(node, "path", None)
        if path:
            return f"Original content: skill package at `{path}` | To restore: `ls -la {path}`"
        return None

    if node_type == "Knowledge":
        content = getattr(node, "knowledge_content", None)
        if content:
            truncated = content[:DEFAULT_IR_KNOWLEDGE_MAX_LEN]
            if len(content) > DEFAULT_IR_KNOWLEDGE_MAX_LEN:
                truncated += "\n... [truncated]"
            return f"Original content: knowledge snippet\n```\n{truncated}\n```"
        return None

    if node_type == "Tool":
        params = getattr(node, "tool_params", None)
        returns = getattr(node, "tool_returns", None)
        if params or returns:
            parts = []
            if params:
                parts.append(f"params: {params}")
            if returns:
                parts.append(f"returns: {returns}")
            return f"Original content: {'; '.join(parts)}"
        return None

    return None


def _render_single_node(node: DataNode) -> str:
    """
    将单个 DataNode 渲染为一行摘要文本，并附带原始内容恢复指引。
    """
    schema = node.get_schema()
    label = schema.get("label", "")
    desc = schema.get("description", "") or ""
    node_class_name = node.__class__.__name__
    node_type = node_class_name.replace("Node", "") if node_class_name.endswith("Node") else node_class_name
    snapshot = render_data_node_snapshot(
        DataNodeRenderSnapshot(
            node_type=node_type,
            label=label,
            desc=desc,
        )
    )
    content_hint = _build_original_content_hint(node)
    if content_hint:
        return f"{snapshot}\n  {content_hint}"
    return snapshot


def assign_turn_indices(messages: list[AnyMessage]) -> list[int]:
    """为每条消息分配 turn 编号。每个 AIMessage 开始一个新 turn。

    Returns:
        与 messages 等长的 turn 编号列表（从 0 开始递增）
    """
    indices: list[int] = []
    current_turn = -1
    for msg in messages:
        if isinstance(msg, AIMessage):
            current_turn += 1
        indices.append(max(current_turn, 0))
    return indices


def should_replace(turn_index: int, max_turn: int, recent_turns: int = DEFAULT_IR_RECENT_TURNS) -> bool:
    """判断该 turn 是否应该被 IR 替换。"""
    return (max_turn - turn_index) >= recent_turns


def try_replace_with_ir(msg: ToolMessage, context: Context) -> ToolMessage:
    """尝试用 IR 摘要替换 ToolMessage 内容。失败时返回原始消息。"""
    tool_call_id = getattr(msg, "tool_call_id", None)
    if not tool_call_id:
        return msg

    tool_name = getattr(msg, "name", None) or "unknown"
    action_label = f"Action({tool_call_id})"

    try:
        data_nodes: list[DataNode] = context.get_next_data_node(action_label)
    except (ValueError, KeyError):
        logger.debug(f"IR consumer: Action node '{action_label}' not found in trajectory, keeping original content")
        return msg

    if not data_nodes:
        return msg

    summary = render_ir_summary(data_nodes, tool_name)
    return ToolMessage(
        content=summary,
        tool_call_id=tool_call_id,
        name=tool_name,
    )
