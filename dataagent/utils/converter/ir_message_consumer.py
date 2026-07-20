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
from typing import TYPE_CHECKING, Any, cast

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from loguru import logger

from dataagent.utils.constants import (
    DEFAULT_IR_KNOWLEDGE_MAX_LEN,
    DEFAULT_IR_RECENT_TURNS,
    DEFAULT_IR_SCRIPT_MAX_LEN,
)

if TYPE_CHECKING:
    from dataagent.core.context.context import Context
    from dataagent.core.context.context_ir import ActionNode, DataNode, KnowledgeNode, QueryNode, StateNode


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


def build_ir_candidate(
    messages: list[AnyMessage],
    context: Context,
    *,
    ir_recent_turns: int = DEFAULT_IR_RECENT_TURNS,
) -> list[AnyMessage]:
    """构建批量 IR candidate，不修改输入消息或历史 state。

    ``ir_recent_turns`` 只决定 ToolMessage 是否具备 IR 替换资格。候选列表与
    输入列表等长，非 ToolMessage、recent turn 和缺少 IR 的消息保持原样。
    """
    if not messages:
        return []

    turn_indices = assign_turn_indices(messages)
    max_turn = max(turn_indices) if turn_indices else 0
    candidate: list[AnyMessage] = []

    for index, message in enumerate(messages):
        if not isinstance(message, ToolMessage):
            candidate.append(message)
            continue
        if not should_replace(turn_indices[index], max_turn, recent_turns=ir_recent_turns):
            candidate.append(message)
            continue

        replaced = try_replace_with_ir(message, context)
        if replaced is message:
            candidate.append(message)
            continue
        candidate.append(message.model_copy(update={"content": replaced.content}))

    return candidate


def try_replace_with_ir(msg: ToolMessage, context: Context) -> ToolMessage:
    """尝试用 IR 摘要替换 ToolMessage 内容。失败时返回原始消息。

    P1 (stable IR replacement): 首次成功渲染的 IR 摘要按 ``tool_call_id``
    缓存在 ``context.ir_summary_cache`` 上，后续调用直接复用缓存内容，
    避免 trajectory 增长导致中段消息内容变化而截断 DeepSeek 前缀缓存。
    仅当 ``context`` 暴露了真实 ``dict`` 类型的缓存时启用；MagicMock 等
    无此属性的对象自动退化为原始无缓存行为。
    """
    tool_call_id = getattr(msg, "tool_call_id", None)
    if not tool_call_id:
        return msg

    ir_cache = getattr(context, "ir_summary_cache", None)
    if isinstance(ir_cache, dict):
        cached = ir_cache.get(tool_call_id)
        if cached is not None:
            return ToolMessage(
                content=cached,
                tool_call_id=tool_call_id,
                name=getattr(msg, "name", None) or "unknown",
            )

    tool_name = getattr(msg, "name", None) or "unknown"
    action_label = f"Action({tool_call_id})"

    try:
        data_nodes: list[DataNode] = context.get_next_data_node(action_node_label=action_label)
    except (ValueError, KeyError):
        logger.debug(f"IR consumer: Action node '{action_label}' not found in trajectory, keeping original content")
        return msg

    if not data_nodes:
        return msg

    summary = render_ir_summary(data_nodes, tool_name)

    if isinstance(ir_cache, dict):
        ir_cache[tool_call_id] = summary

    return ToolMessage(
        content=summary,
        tool_call_id=tool_call_id,
        name=tool_name,
    )


def build_query_and_instruction_text(context: Context) -> str:
    """
    Build the query and instruction text to infer perfect state.

    Args:
        context(Context): The context object.

    Returns:
        str: The query and instruction text.
    """
    query_node_label = context.initial_pt or ""
    query_ir = cast("QueryNode", context.get_IR_from_node(graph_node_label=query_node_label))
    user_query = query_ir.query
    traj = context.get_trajectory()
    decendent_nodes = traj.successors(query_node_label)
    user_instruction = ""
    for node in decendent_nodes:
        if node.startswith("Knowledge"):
            knowledge_ir = cast("KnowledgeNode", context.get_IR_from_node(graph_node_label=node))
            user_instruction = knowledge_ir.knowledge_content
            break

    return f"query: {user_query}\nuser_instruction: {user_instruction}"


def build_past_perfect_state(context: Context) -> tuple[dict[str, str], str]:
    """
    Build the past perfect state text to infer perfect state.

    Args:
        context(Context): The context object.

    Returns:
        tuple[dict[str, str], str]: The past perfect state dictionary and text.
    """
    active_branch = sorted(context.get_active_branch())
    if not active_branch:
        return {}, ""
    active_nodes = active_branch[0]
    traj = context.get_trajectory(trimmed=True)
    past_state_nodes = traj.predecessors(active_nodes)
    for node in past_state_nodes:
        if node.startswith("State"):
            state_ir = cast("StateNode", context.get_IR_from_node(graph_node_label=node))
            state_string = (
                "goal_intent: {goal}\n"
                "belief_about_world: {belief}\n"
                "action_history_summary: {action_history}\n"
                "current_position: {current_status}\n"
                "user_feedback_state: {feedback}\n"
                "epistemic_state: {uncertainty}"
            ).format(
                goal=state_ir.goal or "",
                belief=state_ir.belief or "",
                action_history=state_ir.action_history or "",
                current_status=state_ir.current_status or "",
                feedback=state_ir.feedback or "",
                uncertainty=state_ir.uncentainty or "",
            )
            state_dict = {
                "goal_intent": state_ir.goal or "",
                "belief_about_world": state_ir.belief or "",
                "action_history_summary": state_ir.action_history or "",
                "current_position": state_ir.current_status or "",
                "user_feedback_state": state_ir.feedback or "",
                "epistemic_state": state_ir.uncentainty or "",
            }
            return state_dict, state_string
    return {}, ""


def build_past_action(context: Context) -> str:
    """
    Build the past action text to infer perfect state.

    Args:
        context(Context): The context object.

    Returns:
        str: The past action text.
    """
    active_nodes = context.get_active_branch()
    if any(not i.startswith("Action") for i in active_nodes):
        return ""

    summaries: list[str] = []
    for idx, action_label in enumerate(active_nodes):
        ir = cast("ActionNode", context.get_IR_from_node(graph_node_label=action_label))
        try:
            data_nodes: list[DataNode] = context.get_next_data_node(action_node_label=action_label)
        except Exception:
            data_nodes = []

        header = f"[IR Summary {idx:02d}] tool={ir.action}, input={ir.params}, success={ir.success}, output={ir.output}"
        lines = [header, "Artifacts produced:"]
        if data_nodes:
            for node in data_nodes:
                line = _render_single_node(node)
                if line:
                    lines.append(f"- {line}")
        else:
            lines.append("- (no artifacts)")

        summaries.append("\n".join(lines))

    return "\n\n".join(summaries)


def build_available_actions(*, runtime: Any = None) -> str:
    """
    Build the available actions text to infer perfect state.

    Returns:
        The available actions text.
    """
    tm = getattr(runtime, "tool_manager", None) if runtime is not None else None
    if tm is None:
        return ""

    available_actions_str = ""
    for action in tm.list_tools():
        action_schema = tm.get_schema(action)
        available_actions_str += f"{action}: {action_schema.description}\n"

    return available_actions_str


def build_history_context(context: Context) -> str:
    """
    Build the history context text to infer perfect state.

    Args:
        context(Context): The context object.

    Returns:
        str: The history context text.
    """
    from dataagent.utils.converter.graph_summary import load_run_summary_messages

    historical_messages = load_run_summary_messages(context, context.get_all_historical_trajectories())
    return "\n".join(str(message.content) for message in historical_messages)


def format_data_lineage(context: Context, current_state: dict[str, str] | None = None) -> str:
    """
    Format the data lineage as text.

    Args:
        context(Context): The context object.
        current_state(dict[str, str] | None): The current state of the agent (Default: `None`).

    Returns:
        str: The formatted data lineage as text.
    """
    if current_state is None:
        current_state = {}

    blocks: list[str] = []
    groups = context.get_lineage(text_file_only=True)
    for group in groups:
        if not group:
            continue

        path = getattr(group[0][0], "path", None) or ""
        lines = [f"[IR Lineage] path: {path}"]
        for data_ir, action_ir, state_ir in group:
            data_type = data_ir.__class__.__name__.replace("Node", "")
            desc = data_ir.description or ""
            if action_ir is None:  # case 1: action_ir is None
                agent_status = "已收到用户query"
                source_from = "user upload"
            else:  # case 2: action_ir is not None
                agent_status = state_ir.current_status if state_ir else current_state.get("current_position", "")
                source_from = action_ir.action if action_ir else ""

            lines.append(
                f"- {data_type}({data_ir.label}), description: {desc}; "
                f"agent_status: {agent_status}; source_from: {source_from};"
            )

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def get_recent_read_files(context: Context) -> set[str]:
    """
    Get the recent read files.

    Args:
        context(Context): The context object.

    Returns:
        set[str]: The recent read file paths.
    """
    import networkx as nx

    from dataagent.core.context.utils_context_filesystem import lineage_path_key

    recent_read_files: set[str] = set()
    if context.initial_pt is None or not context.get_active_branch():
        return recent_read_files

    if DEFAULT_IR_RECENT_TURNS <= 0:
        return recent_read_files

    G = context.get_trajectory(trimmed=True)
    if G.number_of_nodes() == 0:
        return recent_read_files

    ordered = [str(n) for n in nx.topological_sort(G)]
    recent_states = [n for n in ordered if n.startswith("State")][-DEFAULT_IR_RECENT_TURNS:]
    action_labels = [a for s in recent_states for a in G.successors(s) if a.startswith("Action")]
    for a in action_labels:
        action_ir = cast("ActionNode", context.get_IR_from_node(graph_node_label=a))
        if action_ir.action == "read_file":
            path = action_ir.params.get("path")
            if path:
                recent_read_files.add(lineage_path_key(p=str(path)))

    return recent_read_files
