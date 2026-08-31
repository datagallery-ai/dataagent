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

import re
from typing import TYPE_CHECKING, Any

import networkx as nx
from loguru import logger

from dataagent.core.context.utils_context_filesystem import extract_file_paths_from_query

if TYPE_CHECKING:
    from dataagent.core.context.context import Context


class TrajectoryEditor:
    """Mutations to IR + current-run trajectory graph (register / remove / modify / pointers)."""

    def __init__(self, *, ctx: Context) -> None:
        self._ctx = ctx

    @staticmethod
    def infer_edge_type(*, edge_type: str | None, predecessor_node: list[str], node_type: str) -> str:
        """
        Infer edge type when the caller does not provide one.

        Args:
            edge_type (str | None): User-provided edge type, if any.
            predecessor_node (list[str]): Predecessor graph node ids.
            node_type (str): Type of the node being registered.

        Returns:
            str: Inferred relationship / edge type string.
        """
        if edge_type is not None:
            return edge_type

        if node_type == "Response":
            return "has_response"

        if (
            all(i.startswith("Query") for i in predecessor_node) or all(i.startswith("State") for i in predecessor_node)
        ) and node_type == "Action":
            return "triggers"

        if (
            all(i.startswith("Query") for i in predecessor_node)
            or all(i.startswith("Action") for i in predecessor_node)
        ) and node_type == "State":
            return "has_conclusion"

        if all(i.startswith("State") for i in predecessor_node) and node_type == "State":
            return "has_conclusion"

        raise ValueError("No default edge type detected. Please provide an edge type.")

    def update_node_counts_from_label(self, *, node_type: str, label: str) -> None:
        """
        Update node_counts based on the sequence number extracted from label.
        Label format: f"{node_type.lower()}{sequence_number}" (e.g., "query00000", "action00001").

        Args:
            node_type (str): node type
            label (str): node label
        """
        if node_type not in self._ctx.state.node_counts:
            return

        node_type_lower = node_type.lower()
        if label.startswith(node_type_lower):
            try:
                number_str = label.strip(node_type_lower)
                sequence_number = int(number_str)
                self._ctx.state.node_counts[node_type] = max(
                    self._ctx.state.node_counts[node_type], sequence_number + 1
                )
            except (ValueError, IndexError):
                pass

    def register_query(self, *, query: str, additional_files: list[str]) -> str:
        """
        Initialize trajectory graph by registering user query.

        Args:
            query (str): user query in this run
            addtional_files (list[str]): any additional files from user upload

        Returns:
            str: Registered query nodes name in the form f"Query(query{sequence_number})".
        """
        logger.debug(
            f"Context: Registering query for user={self._ctx.state.user_id}, session={self._ctx.state.session_id}, "
            f"run={self._ctx.state.run_id}, sub={self._ctx.state.sub_id}. Query='{query[:50]}...'"
        )
        if self._ctx.state.initial_pt is not None:
            raise RuntimeError("Agent cannot have more than one query in one run!")

        sequence_number: str = str(self._ctx.state.node_counts["Query"]).zfill(5)
        query_node = f"Query(query{sequence_number})"
        self._ctx.state.ir.add_IR(
            node_type="Query",
            label="query" + sequence_number,
            description="User query No." + sequence_number,
            user_id=self._ctx.state.user_id,
            session_id=self._ctx.state.session_id,
            run_id=self._ctx.state.run_id,
            workspace_root=self._ctx.state.workspace,
            config=self._ctx.state.config,
            query=query,
            additional_files=additional_files,
            raw_user_query=query,
        )
        self._ctx.state.trajectory.add_node(
            node_for_adding=query_node,
            node_type="Query",
            description="User query No." + sequence_number,
            query=query,
            additional_files=additional_files,
            raw_user_query=query,
            run_id=self._ctx.state.run_id,
        )
        self._ctx.state.node_counts["Query"] += 1
        self._ctx.state.initial_pt = query_node
        self._ctx.state.current_pt.add(query_node)
        file_paths = extract_file_paths_from_query(query=query)
        node_labels: list[str] = []
        for file_path in file_paths.get("File", []):
            node_label = self.register_node(
                node_type="File",
                description="",
                predecessor_node=[f"Query(query{sequence_number})"],
                edge_type="has_additional_file",
                path=file_path,
                source="user_upload",
            )
            node_labels.append(node_label)

        for table_path in file_paths.get("Table", []):
            node_label = self.register_node(
                node_type="Table",
                description="",
                predecessor_node=[f"Query(query{sequence_number})"],
                edge_type="has_additional_table",
                path=table_path,
            )
            node_labels.append(node_label)

        self.modify_node(graph_node_label=query_node, changes={"additional_files": node_labels})
        self._bridge_to_latest_history(query_node=query_node)
        if self._ctx.state.session_root_pt is None:
            self._ctx.state.session_root_pt = query_node

        return query_node

    def register_node(
        self,
        *,
        node_type: str,
        description: str,
        predecessor_node: list[str],
        edge_type: str | None = None,
        label: str | None = None,
        add_pt: bool = False,
        remove_pt: bool = False,
        **kwargs: Any,
    ) -> str:
        """
        Add node to IRManager and to current trajectory graph.

        Args:
            node_type (str): node type
            description (str): node description
            predecessor_node (list[str]): alias of its predecessor node
            edge_type (Optional[str]): edge type between incoming node and its predecessor node (Default: `None`)
            label (Optional[str]): IR label (Default: `f{node_type in lower case}{five digits}`)
            add_pt (bool): whether to add a new point or not; if True, a pointer will be added to the incoming node \
                (Default: `False`)
            remove_pt (bool): whether to remove a pointer or not; if True, a pointer to the predecessor node will be \
                removed (Default: `False`)
            **kwargs: additional parameters to be passed to self._ctx_._IR.add_IR: \
                - additional: follow comments in IRManager.add_IR()

        Return:
            str: Registered node name in the form f"{node_type}({label})".
        """
        if not self._ctx.state.initial_pt:
            raise RuntimeError("Cannot register other node before registering query node.")

        self._validate_predecessor_nodes(predecessor_node=predecessor_node)
        edge = self.infer_edge_type(edge_type=edge_type, predecessor_node=predecessor_node, node_type=node_type)
        if label is None:
            label = node_type.lower() + str(self._ctx.state.node_counts[node_type]).zfill(5)

        self._ctx.state.ir.add_IR(
            node_type=node_type,
            label=label,
            description=description,
            user_id=self._ctx.state.user_id,
            session_id=self._ctx.state.session_id,
            run_id=self._ctx.state.run_id,
            workspace_root=self._ctx.state.workspace,
            config=self._ctx.state.config,
            **kwargs,
        )
        node_name = f"{node_type}({label})"
        self._ctx.state.trajectory.add_node(
            node_for_adding=node_name,
            node_type=node_type,
            description=description,
            run_id=self._ctx.state.run_id,
            **kwargs,
        )
        for i in predecessor_node:
            self._ctx.state.trajectory.add_edge(u_of_edge=i, v_of_edge=node_name, edge_type=edge)

        self._ctx.state.node_counts[node_type] += 1
        self._update_current_pointer(
            node_type=node_type, predecessor_node=predecessor_node, label=label, add_pt=add_pt, remove_pt=remove_pt
        )
        return node_name

    def remove_node(self, *, graph_node_label: str) -> None:
        """
        Remove a node in IRManager and in current trajectory graph. If the node is a part of the current pointer, the
        previous nodes will be added to the current pointer.

        Args:
            graph_node_label (str): graph node label, assumed to be in the form f"{node_type}({label})".
        """
        re_match: re.Match | None = re.fullmatch(r"(.+)\((.+)\)", graph_node_label)
        if re_match is None:
            raise ValueError(f"Graph node label '{graph_node_label}' has illegal format.")

        if graph_node_label in self._ctx.state.current_pt:
            self._ctx.state.current_pt.remove(graph_node_label)
            for i in self._ctx.state.trajectory.predecessors(graph_node_label):
                self._ctx.state.current_pt.add(i)

        node_type: str = re_match.group(1)
        label: str = re_match.group(2)
        self._ctx.state.ir.remove_IR(label=label, node_type=node_type)
        self._ctx.state.trajectory.remove_node(graph_node_label)
        if not nx.is_weakly_connected(G=self._ctx.state.trajectory):
            logger.error(f"Trajectory becomes disconnected after removing graph node {graph_node_label}.")

    def modify_node(self, *, graph_node_label: str, changes: dict[str, Any]) -> None:
        """
        Modify node in IRManager and in current trajectory graph.

        Args:
            graph_node_label (str): graph node label, assumed to be in the form f'{node_type}({label})'
            changes (dict[str, Any]): changes to be applied, keys being node attributes and values being changes
        """
        logger.debug(f"Context: Modifying node={graph_node_label} with changes={changes}")
        re_match: re.Match | None = re.fullmatch(r"(.+)\((.+)\)", graph_node_label)
        if re_match is None:
            raise ValueError(f"Graph node label '{graph_node_label}' has illegal format.")

        node_type: str = re_match.group(1)
        label: str = re_match.group(2)
        self._ctx.state.ir.modify_IR(label=label, node_type=node_type, changes=changes)
        for attr, value in changes.items():
            self._ctx.state.trajectory.nodes[graph_node_label][attr] = value

    def add_edge(self, *, from_node: str, to_node: str, edge_type: str) -> None:
        """
        Add an edge manually to the current context.

        Args:
            from_node (str): label of the from node
            to_node (str): label of the to node
            edge_type (str): type of the edge
        """
        try:
            self._ctx.state.trajectory.add_edge(u_of_edge=from_node, v_of_edge=to_node, edge_type=edge_type)
        except Exception as e:
            logger.error(f"Failed to add edge from {from_node} to {to_node} with edge type {edge_type}: {e}")

    def _validate_predecessor_nodes(self, *, predecessor_node: list[str]) -> None:
        """
        Validate the predecessor nodes of the new node.

        Args:
            predecessor_node (list[str]): list of predecessor nodes.
        """
        if not predecessor_node:
            raise ValueError("At least one predecessor node is required.")

        for i in predecessor_node:
            if i not in list(self._ctx.state.trajectory):
                raise ValueError(f"Cannot find predecessor node {i} on the graph.")

        for pred_node in predecessor_node:
            predecessor_attrs = self._ctx.state.trajectory.nodes[pred_node]
            predecessor_run_id = predecessor_attrs.get("run_id")
            if predecessor_run_id is not None and predecessor_run_id < self._ctx.state.run_id:
                raise ValueError(
                    f"Cannot create edge from current run (run_id={self._ctx.state.run_id}) node to historical node "
                    f"{pred_node} (run_id={predecessor_run_id}). Current run's nodes should not connect to historical."
                )

    def _update_current_pointer(
        self,
        *,
        node_type: str,
        predecessor_node: list[str],
        label: str,
        add_pt: bool = False,
        remove_pt: bool = False,
    ) -> None:
        """
        Update current pointer by according to given input parameters.

        Args:
            node_type (str): current node type
            perdecessor_node (list[str]): list of predecessor nodes
            label (str): current node label
            add_pt (bool): whether to add a new point or not
            remove_pt (bool): whether to remove a pointer or not
        """
        new_node = f"{node_type}({label})"
        if add_pt:
            self._ctx.state.current_pt.add(new_node)
        elif remove_pt:
            for i in predecessor_node:
                self._ctx.state.current_pt.remove(i)
        elif node_type in ["State", "Action"]:
            self._ctx.state.current_pt.add(new_node)
            for i in predecessor_node:
                self._ctx.state.current_pt.remove(i)

    def _bridge_to_latest_history(self, *, query_node: str) -> None:
        """
        Connect leaves of the latest restored historical run to this run's new Query node.

        Args:
            query_node (str): This run's new Query node name on the graph.
        """
        if not self._ctx.state.restored or not self._ctx.state.historical_trajectories:
            return

        latest_rid = max(self._ctx.state.historical_trajectories.keys())
        latest = self._ctx.state.historical_trajectories.get(latest_rid)
        if latest is None:
            return

        traj = self._ctx.state.trajectory
        leaves = [n for n in latest.nodes if latest.out_degree(n) == 0]
        for leaf in leaves:
            if leaf in traj and query_node in traj and (leaf, query_node) not in traj.edges:
                traj.add_edge(leaf, query_node, relationship="continues_to", edge_type="continues_to")
