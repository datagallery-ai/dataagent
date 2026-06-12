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
Build semantic, cached summaries for multi-turn historical trajectories.

Each completed run is summarized once by an LLM and stored on disk. Future
turns only read the cached summary file instead of re-summarizing the same run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

import networkx as nx
from langchain_core.messages import BaseMessage, HumanMessage
from loguru import logger

from dataagent.utils.compression_utils import direct_fold
from dataagent.utils.converter.ir_message_consumer import DataNodeRenderSnapshot, render_data_node_snapshot

if TYPE_CHECKING:
    from dataagent.core.context.context import Context

DATA_NODE_PREFIXES = ("Table", "Column", "Knowledge", "Tool", "Script", "File", "Skill")
INTERNAL_NODE_PREFIXES = ("Table",)
# 历史 run 摘要缓存文件后缀，避免每轮重复调模型总结同一段历史。
SUMMARY_FILE_SUFFIX = "_summary.md"


def _extract_node_label(node_id: str) -> str:
    """Extract the inner label from a graph node id like Query(query00000)."""
    match = re.fullmatch(r"(.+)\((.+)\)", node_id)
    return match.group(2) if match else node_id


def _get_data_node_ids_from_graph(
    graph: nx.DiGraph,
    node_label: str,
) -> list[str]:
    """Get data-node ids for successors of node_label within the given historical graph."""
    successors = list(graph.successors(node_label))
    result: list[str] = []
    for label in successors:
        for prefix in DATA_NODE_PREFIXES:
            if label.startswith(prefix):
                result.append(label)
                break
        for prefix in INTERNAL_NODE_PREFIXES:
            if label.startswith(prefix):
                result.extend(_get_data_node_ids_from_graph(graph, label))
                break
    return result


def _render_single_data_node(graph: nx.DiGraph, node_id: str) -> str:
    """Render one historical graph data node without consulting the live context."""
    node_snapshot = graph.nodes[node_id]
    return render_data_node_snapshot(
        DataNodeRenderSnapshot(
            node_type=str(node_snapshot.get("node_type", "") or ""),
            label=_extract_node_label(node_id),
            desc=node_snapshot.get("description", "") or "",
        )
    )


def _render_data_nodes(graph: nx.DiGraph, data_node_ids: list[str], tool_name: str) -> str:
    """Render data nodes from the historical graph to artifact text."""
    lines: list[str] = ["Artifacts produced:"]
    for node_id in data_node_ids:
        line = _render_single_data_node(graph, node_id)
        if line:
            lines.append(f"- {line}")
    if len(lines) == 1:
        lines.append("- (no artifacts)")
    return f"tool={tool_name}\n" + "\n".join(lines)


def _render_single_trajectory_trace(
    run_id: int,
    graph: nx.DiGraph,
) -> str:
    """Render one run into a verbose trace that can be semantically folded by an LLM."""
    lines: list[str] = [f"[Session history - Run {run_id}]"]

    query_nodes = [n for n in graph.nodes() if graph.nodes[n].get("node_type") == "Query"]
    if not query_nodes:
        return "\n".join(lines + ["(no query)"])

    initial_pt = query_nodes[0]
    query_text = graph.nodes[initial_pt].get("query", "(unknown)")
    lines.append(f"User query: {query_text}")

    visited: set[str] = set()
    queue: list[str] = [initial_pt]
    while queue:
        node_id = queue.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)
        node_attrs = graph.nodes[node_id]
        node_type = node_attrs.get("node_type", "")

        if node_type == "Action":
            action_name = node_attrs.get("action", "unknown")
            params = node_attrs.get("params", {})
            output = node_attrs.get("output", "")
            success = node_attrs.get("success", False)
            params_str = str(params) if params else ""
            action_line = f"Action: {action_name}({params_str})"
            if output and output != "Pending":
                action_line += f" -> output: {output}"
            if success is False and output:
                action_line += " [failed]"
            lines.append(action_line)

            data_node_ids = _get_data_node_ids_from_graph(graph, node_id)
            if data_node_ids:
                lines.append(_render_data_nodes(graph, data_node_ids, action_name))

        elif node_type == "State":
            state_text = node_attrs.get("content") or node_attrs.get("state", "")
            if state_text:
                lines.append(f"State: {state_text}")

        for succ in graph.successors(node_id):
            if succ not in visited:
                queue.append(succ)

    return "\n".join(lines)


def get_summary_cache_path(context: Context, run_id: int, sub_id: int = 0) -> Path:
    """Return the on-disk cache path for a run summary."""
    user_id = context.state.user_id
    session_id = context.state.session_id
    if not user_id or not session_id:
        raise ValueError("Context is missing user_id/session_id required for summary cache path.")
    return (
        Path.home()
        / ".dataagent"
        / str(user_id)
        / str(session_id)
        / ".context"
        / f"Run{run_id}_Sub{sub_id}{SUMMARY_FILE_SUFFIX}"
    )


def _create_and_cache_run_summary(
    context: Context,
    *,
    run_id: int,
    sub_id: int,
    graph: nx.DiGraph,
) -> HumanMessage | None:
    """Create a semantic run summary once and persist it beside the context files."""
    if graph.number_of_nodes() == 0:
        return None

    trace_text = _render_single_trajectory_trace(run_id, graph)
    folded_messages = cast(
        list[BaseMessage],
        direct_fold(
            [HumanMessage(content=trace_text)],
        ),
    )
    if not folded_messages:
        return None

    summary_text = str(folded_messages[0].content).strip()
    if not summary_text:
        return None

    content = f"[Session history - Run {run_id}]\n{summary_text}\n"
    cache_path = get_summary_cache_path(context, run_id, sub_id)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(content, encoding="utf-8")
    return HumanMessage(content=content.strip())


def load_run_summary_messages(
    context: Context,
    historical_trajectories: dict[int, nx.DiGraph],
    *,
    sub_id: int = 0,
) -> list[BaseMessage]:
    """Load cached run summaries; create and cache them on demand if missing."""
    if not historical_trajectories:
        return []

    messages: list[BaseMessage] = []
    for run_id in sorted(historical_trajectories.keys()):
        graph = historical_trajectories[run_id]
        if graph.number_of_nodes() == 0:
            continue

        cache_path = get_summary_cache_path(context, run_id, sub_id)
        if not cache_path.exists():
            logger.debug(f"Historical summary cache missing for run={run_id}, sub={sub_id}, creating it now")
            generated = _create_and_cache_run_summary(
                context,
                run_id=run_id,
                sub_id=sub_id,
                graph=graph,
            )
            if generated is not None:
                messages.append(generated)
            continue

        try:
            content = cache_path.read_text(encoding="utf-8").strip()
        except Exception as exc:
            logger.warning(f"Failed to read historical summary cache {cache_path}: {exc}")
            continue

        if content:
            messages.append(HumanMessage(content=content))
    return messages
