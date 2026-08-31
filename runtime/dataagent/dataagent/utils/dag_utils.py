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

from collections.abc import Callable, Iterable

import networkx as nx


def get_root_to_leaf_path(trajectory: nx.DiGraph, leaf_label: str) -> list[str]:
    """
    Get the unique path from the single root (in-degree 0) to the leaf node.

    Assumptions:
        - The graph is a DAG.
        - Each node has at most one predecessor.
        - There is exactly one root (in-degree 0).

    Args:
        trajectory: Directed acyclic graph.
        leaf_label: Node id of the leaf node.

    Returns:
        List of node ids from root to leaf (inclusive).

    Example:
        >>> path = get_root_to_leaf_path(trajectory, "Action(action00003)")
        >>> path
        ['Query(query00000)', 'State(state00000)', 'Action(action00003)']
    """
    if leaf_label not in trajectory:
        raise ValueError(f"Leaf node '{leaf_label}' is not in the graph.")
    if trajectory.out_degree(leaf_label) != 0:
        raise ValueError(f"Node '{leaf_label}' is not a leaf (out-degree != 0).")

    roots = [n for n in trajectory.nodes if trajectory.in_degree(n) == 0]
    root = roots[0]  # Only 1 root in our workflow graph -- querynode

    path: list[str] = [leaf_label]
    visited = {leaf_label}
    current = leaf_label
    while True:
        preds = list(trajectory.predecessors(current))
        if not preds:
            break
        if len(preds) > 1:
            raise ValueError(f"Node '{current}' has more than one predecessor.")
        current = preds[0]
        if current in visited:
            raise ValueError("Cycle detected while walking predecessors.")
        visited.add(current)
        path.append(current)

    if current != root:
        raise ValueError("Leaf is not reachable from the unique root.")

    path.reverse()
    return path


def extract_nodes_by_type_on_path(
    trajectory: nx.DiGraph,
    leaf_label: str,
    node_type: str | None = None,
    *,
    node_type_key: str = "node_type",
    predicate: Callable[[dict], bool] | None = None,
) -> list[str]:
    """
    Extract nodes of a given type on the root-to-leaf path in order.

    Args:
        trajectory: Directed acyclic graph.
        leaf_label: Node id of the leaf node.
        node_type: The target type to match on node attributes.
        node_type_key: Attribute key used for type matching.
        predicate: Optional predicate to filter nodes by attributes.
            If provided, it will be used instead of node_type/node_type_key.

    Returns:
        Ordered list of node ids that satisfy the filter.

    Example:
        >>> nodes = extract_nodes_by_type_on_path(trajectory, "State(state00002)", node_type="Action")
        >>> nodes
        ['Action(action00001)', 'Action(action00002)']
    """
    try:
        path = get_root_to_leaf_path(trajectory, leaf_label)
    except ValueError as e:
        raise ValueError(f"Failed to get root-to-leaf path for leaf node '{leaf_label}': {e}") from e
    if predicate is not None:
        filter_fn: Callable[[dict], bool] = predicate
    else:
        if node_type is None:
            raise ValueError("Either node_type or predicate must be provided.")

        def _type_filter(attrs: dict) -> bool:
            return attrs.get(node_type_key) == node_type

        filter_fn = _type_filter

    return [node_id for node_id in path if filter_fn(trajectory.nodes.get(node_id, {}))]


def extract_attrs_by_type_on_path(
    trajectory: nx.DiGraph,
    leaf_label: str,
    node_type: str,
    *,
    node_type_key: str = "node_type",
    attrs_keys: Iterable[str] | None = None,
) -> list[dict]:
    """
    Extract node attributes of a given type on the root-to-leaf path in order.

    Args:
        trajectory: Directed acyclic graph.
        leaf_label: Node id of the leaf node.
        node_type: The target type to match on node attributes.
        node_type_key: Attribute key used for type matching.
        attrs_keys: If provided, return only these keys for each node.

    Returns:
        Ordered list of node attribute dicts.

    Example:
        >>> attrs = extract_attrs_by_type_on_path(trajectory, "Action(action00003)", "State")
        >>> len(attrs) > 0
        True
    """
    nodes = extract_nodes_by_type_on_path(trajectory, leaf_label, node_type=node_type, node_type_key=node_type_key)
    if attrs_keys is None:
        return [dict(trajectory.nodes[node_id]) for node_id in nodes]
    return [{k: trajectory.nodes[node_id].get(k) for k in attrs_keys} for node_id in nodes]
