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
from typing import TYPE_CHECKING, Any

import networkx as nx
from loguru import logger
from networkx.classes.digraph import DiGraph

from dataagent.utils.runtime_paths import resolve_session_root

if TYPE_CHECKING:
    from dataagent.core.context.context import Context

_IR_KWARGS_SKIP_KEYS: frozenset[str] = frozenset(
    {
        "label",
        "description",
        "user_id",
        "sub_id",
        "session_id",
        "run_id",
        "created_at",
    }
)

# node_link 图节点属性里不应传给 add_IR(**kwargs) 的字段
_TRAJECTORY_RESTORE_SKIP_KEYS: frozenset[str] = _IR_KWARGS_SKIP_KEYS | frozenset(
    {"node_type", "history", "path_backup", "id"}
)


class ContextPersistence:
    """JSON persistence and restoration for trajectory snapshots."""

    def __init__(self, *, ctx: Context) -> None:
        self._ctx = ctx

    @staticmethod
    def _trajectory_json_path(*, user_id: str, session_id: str, run_id: int, sub_id: int) -> str:
        return str(
            resolve_session_root(user_id=user_id, session_id=session_id) / ".context" / f"Run{run_id}_Sub{sub_id}.json"
        )

    @staticmethod
    def _load_trajectory_from_json(*, user_id: str, session_id: str, run_id: int, sub_id: int) -> DiGraph:
        """Load one run's trajectory snapshot (node_link JSON) from disk."""
        store_path = ContextPersistence._trajectory_json_path(
            user_id=user_id, session_id=session_id, run_id=run_id, sub_id=sub_id
        )
        try:
            with open(store_path, encoding="utf-8") as f:
                trajectory_dict = json.load(f)
            return nx.node_link_graph(data=trajectory_dict, edges="edges")
        except Exception as e:
            logger.warning(f"Failed to load context trajectory from JSON file {store_path}: {e}")
            return DiGraph()

    def persist_to_json(self) -> str:
        """
        Persist current run's context (nodes + edges) to a single JSON file.

        Only stores nodes and edges belonging to the current run_id.
        Historical nodes and cross-run bridging edges are reconstructed by
        restore_previous_runs() + register_query() on the next load.

        Returns:
            str: Path to the trajectory snapshot JSON file.
        """
        snapshot_path = ContextPersistence._trajectory_json_path(
            user_id=self._ctx.state.user_id,
            session_id=self._ctx.state.session_id,
            run_id=self._ctx.state.run_id,
            sub_id=self._ctx.state.sub_id,
        )
        context_dir = (
            resolve_session_root(user_id=self._ctx.state.user_id, session_id=self._ctx.state.session_id) / ".context"
        )
        context_dir.mkdir(parents=True, exist_ok=True)

        current_run_trajectory = nx.DiGraph()
        for node, attrs in self._ctx.state.trajectory.nodes(data=True):
            if attrs.get("run_id") == self._ctx.state.run_id:
                current_run_trajectory.add_node(node, **attrs)
        for source, target, attrs in self._ctx.state.trajectory.edges(data=True):
            source_run = self._ctx.state.trajectory.nodes.get(source, {}).get("run_id")
            target_run = self._ctx.state.trajectory.nodes.get(target, {}).get("run_id")
            if source_run == self._ctx.state.run_id and target_run == self._ctx.state.run_id:
                current_run_trajectory.add_edge(source, target, **attrs)

        with open(snapshot_path, "w", encoding="utf-8") as f:
            trajectory_dict = nx.node_link_data(current_run_trajectory, edges="edges")
            json.dump(trajectory_dict, f, indent=4, ensure_ascii=False, default=str)

        logger.debug(f"Persisted context to JSON file: {snapshot_path}")
        return snapshot_path

    def persist_meta_to_json(self) -> str:
        """
        Persist context meta to JSON file, including initial_pt, current_pt and messages.

        Returns:
            str: Path to the JSON file
        """
        savepath = (
            resolve_session_root(user_id=self._ctx.state.user_id, session_id=self._ctx.state.session_id)
            / ".context"
            / f"Run{self._ctx.state.run_id}_Sub{self._ctx.state.sub_id}.meta.json"
        )
        savepath.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "initial_pt": self._ctx.state.initial_pt,
            "current_pt": list(self._ctx.state.current_pt),
            "messages": {},
        }
        msg_snapshot: dict[str, Any] = {}
        try:
            messages = self._ctx.state.messages
            for key in ("pending_branch", "_enriched_plan", "historical_messages"):
                if key in messages:
                    msg_snapshot[key] = messages[key]
        except Exception as e:
            logger.debug(f"Context: snapshot messages for meta failed: {e}")
        else:
            data["messages"] = msg_snapshot

        with open(savepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False, default=str)

        logger.debug(f"Persisted context meta to JSON file: {savepath}")
        return str(savepath)

    def restore_previous_runs(self, *, user_id: str, session_id: str, current_run_id: int, sub_id: int = 0) -> None:
        """
        Restore all previous runs (run_id < current_run_id) for the same session into this Context.
        Idempotent per Context instance via ``self._ctx.state.restored``.
        """
        if self._ctx.state.restored:
            return

        if current_run_id <= 0:
            self._ctx.state.restored = True
            return

        for past_rid in range(current_run_id):
            self._restore_one_past_run(
                past_rid=past_rid,
                user_id=user_id,
                session_id=session_id,
                sub_id=sub_id,
            )

        self._bridge_consecutive_runs(current_run_id=current_run_id)
        self._set_session_root_from_history()
        self._ctx.state.restored = True

    def _restore_one_past_run(self, *, past_rid: int, user_id: str, session_id: str, sub_id: int) -> None:
        """Restore one past run from its trajectory JSON snapshot."""
        graph = self._load_trajectory_from_json(
            user_id=user_id,
            session_id=session_id,
            run_id=past_rid,
            sub_id=sub_id,
        )
        if graph.number_of_nodes() == 0:
            return

        traj = self._ctx.state.trajectory
        for node, attrs in graph.nodes(data=True):
            node_type = str(attrs.get("node_type", "") or "")
            label = str(attrs.get("label", "") or attrs.get("description", "") or "")

            re_match = re.fullmatch(r"(.+)\((.+)\)", str(node))
            if re_match:
                node_type_from_name = re_match.group(1)
                label_from_name = re_match.group(2)
                if not node_type:
                    node_type = node_type_from_name
                if not label or label == str(attrs.get("description", "") or ""):
                    label = label_from_name

            if not node_type or not label:
                continue

            self._ctx.editor.update_node_counts_from_label(node_type=node_type, label=label)
            ir_kwargs = {k: v for k, v in attrs.items() if k not in _TRAJECTORY_RESTORE_SKIP_KEYS}
            try:
                self._ctx.state.ir.add_IR(
                    node_type=node_type,
                    label=label,
                    description=attrs.get("description", ""),
                    user_id=attrs.get("user_id", user_id),
                    session_id=attrs.get("session_id", session_id),
                    run_id=attrs.get("run_id", past_rid),
                    **ir_kwargs,
                )
            except ValueError as e:
                logger.debug(f"Context restore: skip duplicate IR node {node_type}:{label} ({e})")

            if node not in traj:
                traj.add_node(node, **dict(attrs))

        for source, target, edge_attrs in graph.edges(data=True):
            if source in traj and target in traj and (source, target) not in traj.edges:
                traj.add_edge(source, target, **dict(edge_attrs))

        self._ctx.state.historical_trajectories[past_rid] = graph

    def _merge_graph_into_main_trajectory(self, *, graph: DiGraph) -> None:
        """Copy nodes/edges from one run's historical graph into the live session trajectory."""
        traj = self._ctx.state.trajectory
        for node, attrs in graph.nodes(data=True):
            if node not in traj:
                traj.add_node(node, **dict(attrs))
        for u, v, data in graph.edges(data=True):
            if u not in traj or v not in traj:
                continue
            if (u, v) not in traj.edges:
                traj.add_edge(u, v, **dict(data))

    def _bridge_consecutive_runs(self, *, current_run_id: int) -> None:
        """Add continues_to edges from each historical run's leaves to the next run's root Query.

        Bridge source selection:
        - If the previous run has Response node(s), use Response nodes.
        - Otherwise, use out-degree-zero nodes of type Query / State / Action.
        - Other types of leaf nodes are NOT bridged.
        """
        traj = self._ctx.state.trajectory
        hist = self._ctx.state.historical_trajectories
        for past_rid in range(current_run_id):
            if past_rid not in hist:
                continue
            next_rid = past_rid + 1
            next_traj = hist.get(next_rid)
            if next_traj is None:
                continue
            hist_traj = hist[past_rid]

            response_nodes = [n for n in hist_traj.nodes if str(n).startswith("Response(")]
            if response_nodes:
                past_leaves = response_nodes
            else:
                past_leaves = [
                    n
                    for n in hist_traj.nodes
                    if hist_traj.out_degree(n) == 0
                    and any(str(n).startswith(t) for t in ("Query(", "State(", "Action("))
                ]

            next_queries = [n for n in next_traj.nodes if str(n).startswith("Query") and next_traj.in_degree(n) == 0]
            if not next_queries:
                continue
            next_query = next_queries[0]
            for leaf in past_leaves:
                if leaf in traj and next_query in traj and (leaf, next_query) not in traj.edges:
                    traj.add_edge(leaf, next_query, relationship="continues_to", edge_type="continues_to")

    def _set_session_root_from_history(self) -> None:
        """Point session_root_pt at run 0's in-degree-zero Query when present."""
        run0 = self._ctx.state.historical_trajectories.get(0)
        if not run0:
            return
        roots = [n for n in run0.nodes if str(n).startswith("Query") and run0.in_degree(n) == 0]
        if roots:
            self._ctx.state.session_root_pt = roots[0]
