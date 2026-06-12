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

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import networkx as nx
from networkx.classes.digraph import DiGraph

from dataagent.core.context.context_ir import ActionNode, DataNode, IRManager, StateNode
from dataagent.core.context.utils_context_filesystem import md5_file

if TYPE_CHECKING:
    from dataagent.core.context.context import Context


class TrajectoryNavigator:
    """Read-only graph navigation and lineage helpers over current + historical trajectories."""

    def __init__(self, *, ctx: Context) -> None:
        self._ctx = ctx

    def check_run_id(self, *, node_label: str) -> int:
        """
        Return the run id of a given node.

        Args:
            node_label (str): The label of the node in the format of f"{node_type}({label})".

        Returns:
            int: The run_id of the node.
        """
        node = self._ctx.get_IR_from_node(graph_node_label=node_label)
        return node.run_id

    def get_previous_node_label(self, *, node_label: str, edge_filter: list[str] | None = None) -> list[str]:
        """
        Get the previous node label of the given node.

        Args:
            node_label (str): The label of the node in the format of f"{node_type}({label})".
            edge_filter (list[str] | None): The list of edge types to be filtered out. If None, all edge types are
            allowed (Default: `None`).
        Returns:
            list[str]: The previous node label of the given node.
        """
        internal_node_prefix = ["Table"]
        run_id = self.check_run_id(node_label=node_label)
        graph = (
            self._ctx.state.trajectory
            if run_id == self._ctx.state.run_id
            else self._ctx.state.historical_trajectories[run_id]
        )
        predecessors = list(graph.predecessors(node_label))
        if edge_filter:
            predecessors = [
                pred
                for pred in predecessors
                if graph.get_edge_data(pred, node_label, {}).get("edge_type") not in set(edge_filter)
            ]

        for prefix in internal_node_prefix:
            if predecessors and predecessors[0].startswith(prefix):
                return self.get_previous_node_label(node_label=predecessors[0], edge_filter=edge_filter)

        return predecessors

    def get_next_node_label(self, *, node_label: str) -> list[str]:
        """
        Get the next node label of the given node.

        Args:
            node_label (str): The label of the node in the format of f"{node_type}({label})".

        Returns:
            list[str]: The next node label of the given node.
        """
        run_id = self.check_run_id(node_label=node_label)
        if run_id == self._ctx.state.run_id:
            successors = list(self._ctx.state.trajectory.successors(node_label))
        else:
            successors = list(self._ctx.state.historical_trajectories[run_id].successors(node_label))

        return successors

    def action_and_downstream_state_for_data_label(
        self, *, data_node_label: str
    ) -> tuple[ActionNode | None, StateNode | None]:
        """
        From a data graph label, resolve the producing Action (predecessor chain) and the first
        downstream State among that Action's successors. If no such State exists, returns (None, None).

        Args:
            data_node_label (str): The label of the data node in the format of f"{node_type}({label})".

        Returns:
            tuple[ActionNode | None, StateNode | None]: The action and downstream state of the given data node.
        """
        preds = self.get_previous_node_label(node_label=data_node_label)
        if not preds or not preds[0].startswith("Action"):
            return None, None

        action_label = preds[0]
        state_label = next(
            (s for s in self.get_next_node_label(node_label=action_label) if s.startswith("State")), None
        )
        if state_label is None:
            return (cast(ActionNode, self._ctx.get_IR_from_node(graph_node_label=action_label)), None)

        return (
            cast(ActionNode, self._ctx.get_IR_from_node(graph_node_label=action_label)),
            cast(StateNode, self._ctx.get_IR_from_node(graph_node_label=state_label)),
        )

    def get_next_data_node(self, *, action_node_label: str) -> list[DataNode]:
        """
        Get the next data node of the given action node.

        Args:
            action_node_label (str): The label of the action node in the format of f"{node_type}({label})".

        Returns:
            list[DataNode]: The next data node of the given action node.
        """
        if not action_node_label.startswith("Action"):
            raise ValueError(f"Input '{action_node_label}' is not an action node label.")

        self._ctx.get_IR_from_node(graph_node_label=action_node_label)

        internal_node_prefix = ["Table"]
        data_node_prefix = ["Table", "Column", "Knowledge", "Tool", "Script", "File", "Skill"]

        def _collect_data_from_successors(node_label: str) -> list[DataNode]:
            """Collect the data nodes from the successors of the given node."""
            IRs: list[DataNode] = []
            for label in self.get_next_node_label(node_label=node_label):
                for prefix in data_node_prefix:
                    if label.startswith(prefix):
                        IRs.append(cast(DataNode, self._ctx.get_IR_from_node(graph_node_label=label)))

                for prefix in internal_node_prefix:
                    if label.startswith(prefix):
                        IRs.extend(_collect_data_from_successors(node_label=label))

            return IRs

        return _collect_data_from_successors(node_label=action_node_label)

    def trim_trajectory(self) -> DiGraph:
        """
        Trim the trajectory to the shortest path from the initial point to the current point.

        Returns:
            DiGraph: The trimmed trajectory.
        """
        root = self._ctx.state.session_root_pt or self._ctx.state.initial_pt
        if not root or not self._ctx.state.current_pt:
            return cast(DiGraph, self._ctx.state.trajectory.copy())

        active_actions: set[str] = set()
        stack = list(self._ctx.state.current_pt)
        visited: set[str] = set()

        while stack:
            node = stack.pop()
            if node in visited or node not in self._ctx.state.trajectory:
                continue
            visited.add(node)
            if str(node).startswith("Action"):
                active_actions.add(str(node))
            stack.extend(str(pred) for pred in self._ctx.state.trajectory.predecessors(node))

        all_actions = {str(node) for node in self._ctx.state.trajectory.nodes if str(node).startswith("Action")}
        inactive_actions = all_actions - active_actions

        graph = self._ctx.state.trajectory.copy()
        if inactive_actions:
            graph.remove_nodes_from(inactive_actions)

        reachable_nodes = nx.descendants(graph, root)
        reachable_nodes.add(root)
        return cast(DiGraph, graph.subgraph(reachable_nodes).copy())

    def get_lineage(
        self, *, ir: IRManager, text_file_only: bool = False
    ) -> list[list[tuple[DataNode, ActionNode | None, StateNode | None]]]:
        """
        Get the lineage of the data ir.

        Args:
            ir (IRManager): The ir manager.
            text_file_only (bool): Whether to only show text files.

        Returns:
            list[list[tuple[DataNode, ActionNode | None, StateNode | None]]]: The lineage of the data ir.
        """
        data_lineage = ir.show_data_lineage(text_file_only=text_file_only)
        return [
            [
                (
                    cast(DataNode, self._ctx.get_IR_from_node(graph_node_label=item[0])),
                    *self.action_and_downstream_state_for_data_label(data_node_label=item[0]),
                )
                for item in group
            ]
            for group in data_lineage
        ]

    def get_recorded_files(self, *, text_file_only: bool = False) -> dict[str, tuple[str, str]]:
        """
        Return {path: md5} for each data object's latest IR in the lineage.
        MD5 is computed from the latest IR's path_backup when available.

        Args:
            text_file_only (bool): whether to only show text files (Default: `False`)

        Returns:
            dict[str, tuple[str, str]]: {path: (graph_node_label, md5_hex)}
        """
        from dataagent.core.context.utils_context_filesystem import lineage_path_key

        latest_ir_by_path: dict[str, DataNode] = {}
        for group in self.get_lineage(ir=self._ctx.state.ir, text_file_only=text_file_only):
            data_ir = group[0][0]
            path = getattr(data_ir, "path", None)
            if isinstance(path, str) and path:
                latest_ir_by_path[lineage_path_key(p=path)] = data_ir

        out: dict[str, tuple[str, str]] = {}
        for path_key, data_ir in latest_ir_by_path.items():
            backup = getattr(data_ir, "path_backup", "")
            src = backup if isinstance(backup, str) and backup else path_key
            p = Path(src).expanduser()
            if not p.exists() or not p.is_file():
                continue

            graph_node_label = f"{data_ir.__class__.__name__.replace('Node', '')}({data_ir.label})"
            out[path_key] = (graph_node_label, md5_file(p=str(p)))

        return out

    def get_failed_trajectory_and_new_action(self, *, state_node_label: str) -> tuple[DiGraph, list[dict[str, Any]]]:
        """
        Get the failed trajectory and new action of the given node.

        Args:
            graph_node_label (str): graph node label, assumed to be in the form f'{node_type}({label})'

        Returns:
            tuple[DiGraph, list[dict[str, Any]]]: The failed trajectory and new action of the given node.
        """
        if not state_node_label.startswith("State"):
            raise ValueError(f"Input '{state_node_label}' is not a state node label.")

        descendants: set[str] = nx.descendants(self._ctx.state.trajectory, state_node_label)
        descendants.add(state_node_label)
        failed_trajectory = self._ctx.state.trajectory.subgraph(descendants - self._ctx.state.current_pt).copy()
        failed_trajectory = cast(
            DiGraph,
            failed_trajectory.subgraph(nodes=nx.shortest_path(G=failed_trajectory, source=state_node_label)).copy(),
        )
        new_actions: list[dict[str, Any]] = []
        for i in descendants & self._ctx.state.current_pt:
            if not i.startswith("Action"):
                continue

            action_node = cast(ActionNode, self._ctx.get_IR_from_node(graph_node_label=i))
            new_actions.append({"action": action_node.action, "params": action_node.params})

        return failed_trajectory, new_actions

    def subgraph_from_initial_pt(self) -> DiGraph:
        """
        Return the downstream subgraph starting from the current run's initial_pt.

        Falls back to the full trajectory copy when initial_pt is missing or not in the graph.

        Returns:
            DiGraph: The subgraph starting from the current run's initial_pt.
        """
        graph = self._ctx.state.trajectory
        root = self._ctx.state.initial_pt
        if not root or root not in graph:
            return cast(DiGraph, graph.copy())

        reachable = nx.descendants(graph, root)
        reachable.add(root)
        return cast(DiGraph, graph.subgraph(reachable).copy())
