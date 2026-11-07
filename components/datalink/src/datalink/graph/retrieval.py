"""Retrieval interfaces for DataLink — explore, search, get, paths, subgraph."""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from datalink.config import DataLinkConfig
from datalink.graph.storage import GraphStorage
from datalink.models.edge import Edge, EdgeType, PendingEdge
from datalink.models.node import ColumnNode, ConceptNode, EntityNode, Node, NodeType, TableNode
from datalink.models.profile import ColumnProfile
from datalink.utils.credential import (
    build_id_mapping,
    mask_credentials,
    mask_result,
    resolve_masked_id,
)
from datalink.utils.embedding import EmbeddingService
from datalink.utils.ids import parse_column_id
from datalink.utils.sql_ident import dialect_from_source, quote_identifier, quote_qualified

logger = logging.getLogger(__name__)

# ── Explore dataclasses ──────────────────────────────────────────────


@dataclass
class ResolvedNode:
    """A node resolved from query tokens, with match metadata."""

    node_id: str
    name: str
    node_type: NodeType
    match_reason: str  # "name_exact", "name_substring", "semantic_type", "concept_expand", etc.
    relevance_score: float  # 0.0–1.0


@dataclass
class NodeContext:
    """Context built for one resolved node."""

    node: Node
    edges_by_group: dict[str, list[dict[str, Any]]]  # group_key → edge summary list
    profile_summary: dict[str, Any] | None = None  # Column nodes only
    pending_edges: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RelationshipMap:
    """Relationships among resolved nodes."""

    join_relations: list[dict[str, Any]]  # foreign_key + joinable edges
    semantic_relations: list[dict[str, Any]]  # represents/has_concept/semantic_synonym
    statistical_relations: list[dict[str, Any]]  # correlated/distribution_similar
    indirect_paths: list[dict[str, Any]]  # multi-hop paths among resolved nodes


@dataclass
class ExploreBudget:
    """Adaptive output budget, scaled to project size.

    Multi-dimensional budget controlling explore output size and content,
    inspired by codegraph's ExploreOutputBudget design.
    """

    max_output_chars: int
    max_nodes: int
    max_edges_per_node: int
    max_edges_per_relationship_kind: int  # max edges shown per relationship category
    max_sample_values: int
    max_columns_per_table: int  # max columns to show per table group
    max_pending_edges_per_node: int  # max pending edges per node
    include_relationships: bool
    include_additional_nodes: bool
    include_budget_note: bool
    include_completeness_signal: bool  # tell agent "all data included, no further calls needed"
    include_low_confidence_marker: bool  # show ⚠️ for low-confidence matches


@dataclass
class TableGroup:
    """Nodes grouped under one table."""

    table_node: Node
    column_nodes: list[NodeContext]
    ungrouped: bool = False  # for Concept/Entity not under any table


# ── Helper functions ─────────────────────────────────────────────────

# Edge type groupings for relationship map
JOIN_EDGE_TYPES = {EdgeType.FOREIGN_KEY, EdgeType.JOINABLE}
SEMANTIC_EDGE_TYPES = {
    EdgeType.REPRESENTS,
    EdgeType.HAS_CONCEPT,
    EdgeType.SEMANTIC_SYNONYM,
    EdgeType.SEMANTIC_TYPE_MATCH,
}
STATISTICAL_EDGE_TYPES = {EdgeType.CORRELATED, EdgeType.DISTRIBUTION_SIMILAR}


def _describe_missing(pe: PendingEdge) -> str:
    """Generate a human-readable note about missing endpoints of a pending edge."""
    missing = pe.missing_endpoints
    if "source" in missing and "target" in missing:
        return f"Both endpoints not yet in graph (source: {pe.source_id}, target: {pe.target_id})"
    elif "target" in missing:
        return f"Referenced node '{pe.target_id}' not yet in graph"
    elif "source" in missing:
        return f"Source node '{pe.source_id}' not yet in graph"
    else:
        return "Endpoints status unknown"


def get_explore_budget(dataset_count: int, focus: str | None = None) -> ExploreBudget:
    """Calculate adaptive output budget based on project size and focus direction.

    Multi-tier budget scaled to dataset count, with focus-specific adjustments.
    Inspired by codegraph's tiered ExploreOutputBudget design.
    """

    # Base budget by project size — 4 tiers
    if dataset_count < 3:
        budget = ExploreBudget(
            max_output_chars=8000,
            max_nodes=8,
            max_edges_per_node=3,
            max_edges_per_relationship_kind=4,
            max_sample_values=3,
            max_columns_per_table=5,
            max_pending_edges_per_node=2,
            include_relationships=False,
            include_additional_nodes=False,
            include_budget_note=False,
            include_completeness_signal=True,
            include_low_confidence_marker=True,
        )
    elif dataset_count < 10:
        budget = ExploreBudget(
            max_output_chars=12000,
            max_nodes=12,
            max_edges_per_node=5,
            max_edges_per_relationship_kind=6,
            max_sample_values=5,
            max_columns_per_table=8,
            max_pending_edges_per_node=2,
            include_relationships=True,
            include_additional_nodes=False,
            include_budget_note=True,
            include_completeness_signal=True,
            include_low_confidence_marker=True,
        )
    elif dataset_count < 50:
        budget = ExploreBudget(
            max_output_chars=16000,
            max_nodes=15,
            max_edges_per_node=7,
            max_edges_per_relationship_kind=10,
            max_sample_values=5,
            max_columns_per_table=10,
            max_pending_edges_per_node=3,
            include_relationships=True,
            include_additional_nodes=True,
            include_budget_note=True,
            include_completeness_signal=False,
            include_low_confidence_marker=True,
        )
    else:
        budget = ExploreBudget(
            max_output_chars=20000,
            max_nodes=20,
            max_edges_per_node=10,
            max_edges_per_relationship_kind=15,
            max_sample_values=5,
            max_columns_per_table=12,
            max_pending_edges_per_node=3,
            include_relationships=True,
            include_additional_nodes=True,
            include_budget_note=True,
            include_completeness_signal=False,
            include_low_confidence_marker=True,
        )

    # Focus adjustments: redistribute budget without changing total cap
    if focus == "data_profile":
        budget.max_sample_values = min(budget.max_sample_values * 2, 20)
        budget.max_edges_per_node = max(budget.max_edges_per_node // 2, 3)
        budget.max_edges_per_relationship_kind = max(budget.max_edges_per_relationship_kind // 2, 3)
    elif focus == "join_paths":
        budget.max_edges_per_node = min(budget.max_edges_per_node * 2, 20)
        budget.max_edges_per_relationship_kind = min(budget.max_edges_per_relationship_kind * 2, 20)
        budget.max_sample_values = max(budget.max_sample_values // 2, 2)
        budget.include_relationships = True  # always show paths with this focus
    elif focus == "schema":
        budget.max_nodes = min(budget.max_nodes * 2, 40)
        budget.max_columns_per_table = min(budget.max_columns_per_table * 2, 30)
        budget.max_sample_values = 0  # schema focus: just dtype, no samples
        budget.max_edges_per_node = max(budget.max_edges_per_node // 2, 2)

    return budget


def _edge_direction_label(edge: Edge, from_node_id: str) -> str:
    """How an edge reads from the perspective of `from_node_id`."""
    if edge.source_id == from_node_id:
        return "→"
    return "←"


class GraphRetrieval:
    """Four retrieval APIs on top of the GraphStorage layer.

    1. search_nodes — full-text/semantic search
    2. get_node — node detail + adjacent edges
    3. find_paths — graph traversal between two nodes
    4. extract_subgraph — expand a set of nodes by hops
    """

    def __init__(self, storage: GraphStorage, config: DataLinkConfig | None = None):
        """Initialize retrieval with an existing storage instance.

        Args:
            storage: GraphStorage instance to query against.
            config: DataLinkConfig for embedding service initialization.
                    If None, loads from default config file.
                    When embedding.model is empty, vector retrieval is disabled.
        """
        self.storage = storage
        self.config = config or DataLinkConfig.load()
        self._id_mapping: dict[str, str] | None = None

        # Initialize embedding service (optional — disabled when model is empty)
        self.embedding_service: EmbeddingService | None = None
        if self.config.embedding.is_available(self.config.llm):
            self.embedding_service = EmbeddingService(self.config.embedding, self.config.llm)
            logger.info(f"Vector retrieval enabled (model: {self.config.embedding.model})")

    def _ensure_id_mapping(self) -> dict[str, str]:
        """Build or return the masked→real ID mapping from all stored node IDs."""
        if self._id_mapping is None:
            self._id_mapping = build_id_mapping(self.storage.get_all_node_ids())
        return self._id_mapping

    def _resolve_input_id(self, node_id: str) -> str:
        """Resolve a possibly-masked or short-alias input ID to the real ID.

        Resolution order:
        1. Masked ID (contains ***:***@ or ://***) → resolve via mapping
        2. Full ID (starts with table: column: concept: entity: edge:) → pass through
        3. Short alias "table_name.column_name" → find matching column node
        4. Bare name → exact name match across all node types
        5. Not resolved → return as-is (caller will handle "not found")
        """
        # 1. Masked ID resolution
        resolved = resolve_masked_id(node_id, self._ensure_id_mapping())
        if resolved != node_id:
            return resolved

        # 2. Already a valid full-format ID — pass through
        first_segment = node_id.split(":")[0] if ":" in node_id else ""
        if first_segment in ("table", "column", "concept", "entity", "edge"):
            return node_id

        # 3. Short alias: "table_name.column_name"
        if "." in node_id:
            table_name, col_name = node_id.split(".", 1)
            # Search for columns matching the column name
            candidates = self.storage.search_nodes_by_name(col_name, NodeType.COLUMN, 10)
            for c in candidates:
                if isinstance(c, ColumnNode) and c.name == col_name:
                    # Check that the parent table name matches
                    parent_table = self.storage.get_node(c.table_id)
                    if parent_table and parent_table.name == table_name:
                        return c.id

        # 4. Bare name — exact match across all types
        for nt in (NodeType.TABLE, NodeType.COLUMN, NodeType.CONCEPT, NodeType.ENTITY):
            candidates = self.storage.search_nodes_by_name(node_id, nt, 5)
            for c in candidates:
                if c.name == node_id:
                    return c.id

        # 5. Not resolved
        return node_id

    def _resolve_input_ids(self, node_ids: list[str]) -> list[str]:
        """Resolve a list of possibly-masked or short-alias input IDs to real IDs."""
        return [self._resolve_input_id(nid) for nid in node_ids]

    def search_nodes(
        self,
        query: str,
        node_type: NodeType | None = None,
        limit: int = 10,
        mask_credential: bool = True,
    ) -> list[dict[str, Any]]:
        """Search nodes by name (substring match) and optionally by type.

        Also searches properties JSON for semantic_type and other indexed fields.
        When embedding service is available, additionally performs vector
        similarity search and merges results (hybrid retrieval).

        Args:
            query: Search string (substring match on node name).
            node_type: Optional filter by node type (column, table, concept, entity).
            limit: Maximum number of results.

        Returns:
            List of dicts with node info and brief edge summary.
        """
        # ── Text-based search (existing logic) ──
        nodes = self.storage.search_nodes_by_name(query, node_type, limit)

        # Also search by semantic_type in properties
        if not node_type or node_type == NodeType.COLUMN:
            conn = self.storage.conn
            prop_rows = conn.execute(
                "SELECT id, type, name, properties FROM nodes WHERE type = 'column' AND properties LIKE ? LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()

            # Convert rows and add to results (avoid duplicates)
            existing_ids = {n.id for n in nodes}
            for row in prop_rows:
                id_str, type_str, name, properties_json = row
                if id_str not in existing_ids:
                    existing_ids.add(id_str)
                    # Parse properties to check if semantic_type matches
                    try:
                        props = json.loads(properties_json) if properties_json else {}
                        if query.lower() in props.get("semantic_type", "").lower():
                            nodes.append(self.storage._row_to_node(row))
                    except Exception:
                        pass

        # ── Vector-based search (hybrid retrieval) ──
        vec_nodes: dict[str, tuple[Node, float]] = {}  # node_id → (node, similarity)
        if self.embedding_service and self.embedding_service.is_available():
            # Check if embedding vectors exist in the DB
            stored_model = self.storage.get_embedding_model()
            if stored_model and stored_model == self.embedding_service.get_model_name():
                query_embedding = self.embedding_service.compute_embedding(query)
                if query_embedding:
                    # Use a lower threshold for retrieval than for merge pre-filter.
                    # The config similarity_threshold (default 0.75) is designed for
                    # merge candidate filtering, not for retrieval where cross-language
                    # and short-query matches naturally have lower similarity scores.
                    vec_results = self.storage.search_nodes_by_embedding(
                        query_embedding, node_type, limit * 2, threshold=0.35
                    )
                    for node, sim in vec_results:
                        if node.id not in vec_nodes or sim > vec_nodes[node.id][1]:
                            vec_nodes[node.id] = (node, sim)

        # ── Merge text + vector results ──
        # Track text-based node IDs for dedup
        text_node_ids = {n.id for n in nodes}
        has_text_matches = bool(text_node_ids)

        # Add vector-only results (nodes not found by text search)
        # Weight logic:
        # - When text search also found results: vector is supplementary → downweight
        # - When text search found nothing: vector is the ONLY dimension → full weight
        merged_nodes: list[tuple[Node, float]] = []  # (node, score)
        # Text match scores: exact=1.0, substring=0.8
        for node in nodes:
            score = 1.0 if node.name.lower() == query.lower() else 0.8
            merged_nodes.append((node, score))

        # Vector results: only add nodes NOT already found by text search,
        # since text matches are more reliable for exact/substring queries.
        # Vector similarity is a supplementary dimension.
        # When text search found matches, vector results are supplementary → 0.6 weight.
        # When text search found nothing, vector is the only effective dimension → 0.8 weight.
        # (Not 1.0 because vector similarity is inherently less precise than text matches.)
        vec_weight = 0.6 if has_text_matches else 0.8
        if vec_nodes:
            for nid, (node, sim) in vec_nodes.items():
                if nid not in text_node_ids:
                    merged_nodes.append((node, sim * vec_weight))

        # Sort by score descending, truncate to limit
        merged_nodes.sort(key=lambda x: x[1], reverse=True)
        merged_nodes = merged_nodes[:limit]

        # Build result dicts
        results = []
        for node, score in merged_nodes:
            edges = self.storage.get_edges_for_node(node.id)
            result = {
                "id": node.id,
                "type": node.type.value,
                "name": node.name,
                "properties": node.properties,
                "edge_count": len(edges),
                "edges_summary": [
                    {
                        "type": e.type.value,
                        "target_id": e.target_id if e.source_id == node.id else e.source_id,
                        "confidence": e.confidence,
                    }
                    for e in edges[:5]  # Show top 5 edges
                ],
            }
            results.append(result)

        if mask_credential:
            results = mask_result(results)

        return results

    def get_node(
        self,
        node_id: str,
        include_edges: bool = True,
        mask_credential: bool = True,
        max_response_chars: int = 25000,
    ) -> dict[str, Any] | None:
        """Get a node's details and optionally all adjacent edges.

        Args:
            node_id: ID of the node to retrieve (masked IDs and short aliases are auto-resolved).
            include_edges: Whether to include adjacent edge information.
            mask_credential: Whether to mask credentials in output (default True).
            max_response_chars: Maximum response size in chars (default 25000).
                When exceeded, edges are trimmed (low-confidence edges removed first).
                Prevents oversized responses that may be truncated by MCP protocol.

        Returns:
            Dict with node details and edges, or None if node doesn't exist.
        """
        node_id = self._resolve_input_id(node_id)
        node = self.storage.get_node(node_id)
        if node is None:
            return None

        result = {
            "id": node.id,
            "type": node.type.value,
            "name": node.name,
            "properties": node.properties,
        }

        if include_edges:
            edges = self.storage.get_edges_for_node(node_id)

            # For table nodes, skip "contains" edges — they're redundant with column_ids
            if node.type == NodeType.TABLE:
                edges = [e for e in edges if e.type != EdgeType.CONTAINS]

            result["edges"] = []
            for edge in edges:
                # Determine the "other" node in this edge
                is_outgoing = edge.source_id == node_id
                other_id = edge.target_id if is_outgoing else edge.source_id
                other_node = self.storage.get_node(other_id)
                result["edges"].append(
                    {
                        "id": edge.id,
                        "type": edge.type.value,
                        "direction": "outgoing" if is_outgoing else "incoming",
                        "source_id": edge.source_id,
                        "target_id": edge.target_id,
                        "confidence": edge.confidence,
                        "properties": edge.properties,
                        "other_node": {
                            "id": other_id,
                            "name": other_node.name if other_node else "unknown",
                            "type": other_node.type.value if other_node else "unknown",
                        }
                        if other_node
                        else None,
                    }
                )

            # Response size cap: if edges list makes result too large,
            # trim low-confidence edges first
            result_size = len(json.dumps(result, ensure_ascii=False))
            if result_size > max_response_chars and result["edges"]:
                # Sort edges by confidence descending, keep only high-confidence ones
                result["edges"].sort(key=lambda e: e.get("confidence", 0), reverse=True)
                while len(result["edges"]) > 3 and len(json.dumps(result, ensure_ascii=False)) > max_response_chars:
                    result["edges"].pop()

            # Include profile if this is a column node
            if node.type == NodeType.COLUMN:
                profile = self.storage.get_profile_for_column(node_id)
                if profile:
                    result["profile"] = {
                        "id": profile.id,
                        "dtype": profile.dtype,
                        "semantic_type": profile.semantic_type,
                        "null_rate": profile.null_rate,
                        "cardinality": profile.cardinality,
                        "unique_rate": profile.unique_rate,
                        "sample_values": profile.sample_values[:5],
                    }

            # Include pending (dangling) edges as suggested_edges
            pending = self.storage.get_pending_edges_for_node(node_id)
            result["suggested_edges"] = [
                {
                    "id": pe.id,
                    "type": pe.type.value,
                    "source_id": pe.source_id,
                    "target_id": pe.target_id,
                    "confidence": pe.confidence,
                    "missing_endpoints": pe.missing_endpoints,
                    "note": _describe_missing(pe),
                }
                for pe in pending
            ]

        if mask_credential:
            result = mask_result(result)

        return result

    def find_paths(
        self,
        source_id: str,
        target_id: str,
        max_depth: int = 3,
        edge_types: list[EdgeType] | None = None,
        limit: int = 3,
        mask_credential: bool = True,
    ) -> list[dict[str, Any]]:
        """Find paths between two nodes using BFS graph traversal.

        Uses SQLite WITH RECURSIVE CTE for efficient traversal.

        Args:
            source_id: Starting node ID (masked IDs and short aliases are auto-resolved).
            target_id: Destination node ID (masked IDs and short aliases are auto-resolved).
            max_depth: Maximum path length (number of edges).
            edge_types: Optional filter — only traverse edges of these types.
            limit: Maximum number of paths to return (default 3).
            mask_credential: Whether to mask credentials in output (default True).

        Returns:
            List of paths, each containing nodes and edges along the path,
            sorted by total path confidence (descending).
        """
        source_id = self._resolve_input_id(source_id)
        target_id = self._resolve_input_id(target_id)

        # Validate both nodes exist
        source = self.storage.get_node(source_id)
        target = self.storage.get_node(target_id)
        if source is None or target is None:
            return []

        # Always use Python BFS for path finding.
        # SQLite's recursive CTE with json_each for cycle prevention causes
        # "datatype mismatch" errors with long node/edge IDs, and alternative
        # CTE-based cycle prevention (comma-separated strings, nested CTEs)
        # is either unreliable or unsupported inside a recursive CTE. Python
        # BFS is simpler, correct, and fast enough for DataLink's graph size.
        return self._python_bfs_paths(source_id, target_id, max_depth, edge_types, limit)

        # Convert results to structured paths
        paths = []
        for row in rows:
            path_edges_json, confidence, depth = row
            try:
                path_edge_ids = json.loads(path_edges_json)
            except Exception:
                path_edge_ids = []

            # Build detailed path
            path_nodes = [source_id]
            path_edges_detail = []
            current_confidence = 1.0

            for edge_id in path_edge_ids:
                edge = self.storage.get_edge(edge_id)
                if edge:
                    path_edges_detail.append(
                        {
                            "id": edge.id,
                            "type": edge.type.value,
                            "source_id": edge.source_id,
                            "target_id": edge.target_id,
                            "confidence": edge.confidence,
                        }
                    )
                    # Add the next node
                    if edge.source_id == path_nodes[-1]:
                        path_nodes.append(edge.target_id)
                    else:
                        path_nodes.append(edge.source_id)
                    current_confidence *= edge.confidence

            paths.append(
                {
                    "nodes": path_nodes,
                    "edges": path_edges_detail,
                    "length": depth,
                    "confidence": float(confidence),
                }
            )

        if mask_credential:
            paths = mask_result(paths)

        return paths

    def _python_bfs_paths(
        self,
        source_id: str,
        target_id: str,
        max_depth: int,
        edge_types: list[EdgeType] | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Fallback BFS path finder in pure Python.

        Used when the SQL CTE approach fails.
        """
        # BFS to find shortest paths
        visited_paths = []
        queue = [(source_id, [], 1.0)]  # (current_node, edge_path, confidence)

        type_set = {et.value for et in edge_types} if edge_types else None

        while queue:
            current, edge_path, confidence = queue.pop(0)

            if len(edge_path) > max_depth:
                continue

            if current == target_id and edge_path:
                visited_paths.append((edge_path, confidence))
                continue

            # Get edges from current node
            edges = self.storage.get_edges_for_node(current)
            for edge in edges:
                if type_set and edge.type.value not in type_set:
                    continue

                # Determine next node
                next_node = edge.target_id if edge.source_id == current else edge.source_id

                # Avoid cycles
                visited_nodes = {current}
                for ep in edge_path:
                    visited_nodes.add(ep.source_id)
                    visited_nodes.add(ep.target_id)

                if next_node in visited_nodes:
                    continue

                new_path = edge_path + [edge]
                new_confidence = confidence * edge.confidence
                queue.append((next_node, new_path, new_confidence))

        # Sort by confidence (descending)
        visited_paths.sort(key=lambda x: x[1], reverse=True)

        # Convert to result format
        results = []
        for edge_path, confidence in visited_paths[:limit]:
            path_nodes = [source_id]
            path_edges_detail = []
            for edge in edge_path:
                path_edges_detail.append(
                    {
                        "id": edge.id,
                        "type": edge.type.value,
                        "source_id": edge.source_id,
                        "target_id": edge.target_id,
                        "confidence": edge.confidence,
                    }
                )
                if edge.source_id == path_nodes[-1]:
                    path_nodes.append(edge.target_id)
                else:
                    path_nodes.append(edge.source_id)

            results.append(
                {
                    "nodes": path_nodes,
                    "edges": path_edges_detail,
                    "length": len(edge_path),
                    "confidence": confidence,
                }
            )

        return results

    def extract_subgraph(
        self,
        node_ids: list[str],
        max_hops: int = 2,
        mask_credential: bool = True,
    ) -> dict[str, Any]:
        """Extract a subgraph around the specified nodes.

        Starting from the given node IDs, expand max_hops layers
        of neighbors and return all nodes and edges in the expanded subgraph.

        Args:
            node_ids: Starting node IDs (masked IDs are auto-resolved).
            max_hops: Number of neighbor layers to expand.
            mask_credential: Whether to mask credentials in output (default True).

        Returns:
            Dict with 'nodes' and 'edges' lists representing the subgraph.
        """
        node_ids = self._resolve_input_ids(node_ids)
        visited_nodes = set(node_ids)
        visited_edges = set()
        current_layer = set(node_ids)

        for hop in range(max_hops):
            next_layer = set()
            for nid in current_layer:
                edges = self.storage.get_edges_for_node(nid)
                for edge in edges:
                    if edge.id not in visited_edges:
                        visited_edges.add(edge.id)
                        # Add the connected node
                        other_id = edge.target_id if edge.source_id == nid else edge.source_id
                        if other_id not in visited_nodes:
                            visited_nodes.add(other_id)
                            next_layer.add(other_id)

            current_layer = next_layer
            if not current_layer:
                break

        # Collect all nodes and edges
        nodes = []
        for nid in visited_nodes:
            node = self.storage.get_node(nid)
            if node:
                nodes.append(
                    {
                        "id": node.id,
                        "type": node.type.value,
                        "name": node.name,
                        "properties": node.properties,
                    }
                )

        edges = []
        for eid in visited_edges:
            edge = self.storage.get_edge(eid)
            if edge:
                edges.append(
                    {
                        "id": edge.id,
                        "source_id": edge.source_id,
                        "target_id": edge.target_id,
                        "type": edge.type.value,
                        "confidence": edge.confidence,
                        "properties": edge.properties,
                    }
                )

        result = {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "node_count": len(nodes),
                "edge_count": len(edges),
                "hops": max_hops,
            },
        }

        if mask_credential:
            result = mask_result(result)

        return result

    def get_pending_edges(
        self,
        node_id: str | None = None,
        edge_type: EdgeType | None = None,
        limit: int = 50,
        mask_credential: bool = True,
    ) -> list[dict[str, Any]]:
        """List pending (dangling) edges, optionally filtered by node or type.

        Pending edges reference nodes that don't yet exist in the graph —
        typically FK relationships pointing to tables/columns from datasources
        that haven't been added yet.

        Args:
            node_id: Optional — filter to pending edges involving this node (masked IDs auto-resolved).
            edge_type: Optional — filter by edge type.
            limit: Maximum number of results.
            mask_credential: Whether to mask credentials in output (default True).

        Returns:
            List of dicts with pending edge details and missing endpoint info.
        """
        if node_id:
            node_id = self._resolve_input_id(node_id)
        if node_id:
            pending_edges = self.storage.get_pending_edges_for_node(node_id)
        else:
            pending_edges = self.storage.get_all_pending_edges()

        if edge_type:
            pending_edges = [pe for pe in pending_edges if pe.type == edge_type]

        pending_edges = pending_edges[:limit]

        result = [
            {
                "id": pe.id,
                "type": pe.type.value,
                "source_id": pe.source_id,
                "target_id": pe.target_id,
                "confidence": pe.confidence,
                "missing_endpoints": pe.missing_endpoints,
                "note": _describe_missing(pe),
                "properties": pe.properties,
            }
            for pe in pending_edges
        ]

        if mask_credential:
            return mask_result(result)

        return result

    def list_datasets(self, mask_credential: bool = True) -> list[dict[str, Any]]:
        """List all tables/datasets in the graph with basic stats.

        Returns:
            List of dicts with table info.
        """
        tables = self.storage.get_nodes_by_type(NodeType.TABLE)
        results = []

        for table in tables:
            # Get column count via contains edges
            contains_edges = self.storage.get_edges_for_node(table.id, EdgeType.CONTAINS)
            column_count = len(contains_edges)

            # Count inferred edges involving this table's columns
            column_ids = [e.target_id for e in contains_edges]
            inferred_edge_count = 0
            for col_id in column_ids:
                col_edges = self.storage.get_edges_for_node(col_id)
                for ce in col_edges:
                    if ce.type not in (EdgeType.CONTAINS, EdgeType.FOREIGN_KEY):
                        inferred_edge_count += 1

            # Count pending FK edges involving this table's columns
            pending_fk_count = 0
            for col_id in column_ids:
                pending = self.storage.get_pending_edges_for_node(col_id)
                for pe in pending:
                    if pe.type == EdgeType.FOREIGN_KEY:
                        pending_fk_count += 1

            results.append(
                {
                    "id": table.id,
                    "name": table.name,
                    "source": table.properties.get("source", ""),
                    "row_count": table.properties.get("row_count", 0),
                    "column_count": column_count,
                    "inferred_edge_count": inferred_edge_count,
                    "pending_fk_count": pending_fk_count,
                }
            )

        if mask_credential:
            return mask_result(results)

        return results

    # ── Explore: universal retrieval entry point ─────────────────────

    def explore(
        self,
        query: str,
        max_nodes: int | None = None,
        focus: str | None = None,
        mask_credential: bool = True,
    ) -> str:
        """Universal retrieval entry point — answers data questions in one call.

        Takes a natural-language-ish query (keywords, names, short descriptions)
        and returns organized context about relevant nodes, their relationships,
        and data profiles. Internally combines search, get_node, find_paths,
        and profile data into a single formatted text response.

        Args:
            query: Keywords, names, or short descriptions, e.g.
                   "revenue customer_id orders" or "email address fields".
            max_nodes: Maximum nodes to include in detail. None → auto-scaled.
            focus: Optional focus direction:
                   "join_paths" — relationship chains and JOIN paths prioritized,
                   "schema" — table structure overview prioritized,
                   "data_profile" — column fingerprints and quality prioritized,
                   None — balanced output (default).
            mask_credential: Whether to mask credentials in output (default True).
                   When True, database connection strings in source lines are masked.

        Returns:
            Formatted text string — organized by table, with inline
            relationships, profiles, and pending-edge notes.
        """
        dataset_count = self.storage.count_nodes(NodeType.TABLE)
        budget = get_explore_budget(dataset_count, focus)

        # Override max_nodes if caller specified
        if max_nodes is not None:
            budget.max_nodes = min(max_nodes, budget.max_nodes * 2)

        # Step 1: resolve query to node set
        resolved = self._resolve_query(query, budget.max_nodes)
        if not resolved:
            return (
                f'No results found for "{query}". '
                "Try different keywords or check that the graph has data "
                "(run `datalink info`)."
            )

        # Step 2: build context for each resolved node
        contexts = self._build_context(resolved, budget)

        # Step 3: build relationship map among resolved nodes
        node_ids = {rn.node_id for rn in resolved}
        rel_map = self._build_relationship_map(node_ids, budget)

        # Step 4: format output
        output = self._format_output(contexts, rel_map, resolved, budget, mask_credential)

        return output

    # ── Explore internals ────────────────────────────────────────────

    def _resolve_query(self, query: str, limit: int) -> list[ResolvedNode]:
        """Parse query tokens and match against graph nodes via multiple dimensions.

        Multi-dimension matching:
        - Name substring match
        - semantic_type match (in column properties)
        - Concept/Entity name match → edge expansion to columns
        - comment / description match
        - Vector semantic match (if embedding service is available)
        - Table name match → contains expansion to all columns

        Then expand matched nodes along key edge types for higher recall.
        """
        tokens = [t.strip() for t in query.replace(",", " ").split() if t.strip()]
        if not tokens:
            return []

        resolved: dict[str, ResolvedNode] = {}  # node_id → ResolvedNode (dedup)

        for token in tokens:
            token_lower = token.lower()

            # 1. Name substring match — all types
            name_matches = self.storage.search_nodes_by_name(token, None, limit * 2)
            for node in name_matches:
                score = 1.0 if node.name.lower() == token_lower else 0.8
                reason = "name_exact" if score == 1.0 else "name_substring"
                self._add_resolved(resolved, node, reason, score)

            # 2. semantic_type match — column nodes only
            conn = self.storage.conn
            prop_rows = conn.execute(
                "SELECT id, type, name, properties FROM nodes WHERE type = 'column' AND properties LIKE ? LIMIT ?",
                (f"%{token}%", limit * 2),
            ).fetchall()
            for row in prop_rows:
                node = self.storage._row_to_node(row)
                if node.id in resolved:
                    continue
                props = json.loads(row[3]) if row[3] else {}
                st = props.get("semantic_type", "").lower()
                if token_lower in st:
                    self._add_resolved(resolved, node, "semantic_type", 0.7)

            # 3. comment / description match
            for nt_val in ("column", "concept", "entity"):
                desc_rows = conn.execute(
                    "SELECT id, type, name, properties FROM nodes WHERE type = ? AND properties LIKE ? LIMIT ?",
                    (nt_val, f"%{token}%", limit),
                ).fetchall()
                for row in desc_rows:
                    node = self.storage._row_to_node(row)
                    if node.id in resolved:
                        continue
                    props = json.loads(row[3]) if row[3] else {}
                    if nt_val == "column":
                        text_field = props.get("comment", "")
                    else:
                        text_field = props.get("description", "")
                    if token_lower in text_field.lower():
                        self._add_resolved(resolved, node, "comment_description", 0.5)

        # 4. Vector semantic match — embedding similarity search
        # Only active when embedding service is configured and vectors exist in DB.
        # Serves as a supplementary dimension: finds semantically related nodes
        # that text search may miss (e.g., "revenue" ≈ "income", "amount",
        # or "学校" ≈ "schools" for cross-language matching).
        #
        # Use a lower threshold (0.35) than the config similarity_threshold
        # (default 0.75) because: (1) the config threshold is designed for
        # merge candidate filtering, not retrieval; (2) cross-language and
        # short-query vs long-document matches naturally produce lower cosine
        # similarity scores that would be filtered at 0.5 or above.
        if self.embedding_service and self.embedding_service.is_available():
            stored_model = self.storage.get_embedding_model()
            if stored_model and stored_model == self.embedding_service.get_model_name():
                query_embedding = self.embedding_service.compute_embedding(query)
                if query_embedding:
                    vec_results = self.storage.search_nodes_by_embedding(
                        query_embedding, None, limit * 3, threshold=0.35
                    )
                    # When text search found matches, vector results are supplementary.
                    # When text search found nothing, vector is the only effective dimension
                    # and deserves higher weight (especially for cross-language queries).
                    has_text_hits = bool(resolved)
                    base_vec_weight = 0.6 if has_text_hits else 0.8
                    for node, sim in vec_results:
                        if node.id not in resolved:
                            # Prioritize column/table nodes over concept/entity in
                            # vector results: columns are the final output of explore,
                            # while concept/entity only serve as intermediaries for
                            # edge expansion. Without this, concept/entity nodes
                            # (which tend to have higher cosine similarity to the query
                            # because their names are more abstract) can monopolize
                            # the budget and push out the more useful column matches.
                            node_weight = base_vec_weight
                            if node.type in (NodeType.COLUMN, NodeType.TABLE):
                                node_weight = base_vec_weight * 1.2
                            elif node.type in (NodeType.CONCEPT, NodeType.ENTITY):
                                node_weight = base_vec_weight * 0.7
                            self._add_resolved(resolved, node, "vector_semantic", sim * node_weight)

        # 5. Edge expansion — expand matched nodes along key edge types
        expanded_ids: dict[str, ResolvedNode] = {}
        for rn in list(resolved.values()):
            node = self.storage.get_node(rn.node_id)
            if node is None:
                continue

            if node.type == NodeType.CONCEPT:
                # Concept → all columns it represents
                edges = self.storage.get_edges_for_node(node.id, EdgeType.REPRESENTS)
                for e in edges:
                    col_id = e.source_id if e.target_id == node.id else e.target_id
                    col = self.storage.get_node(col_id)
                    if col and col.id not in resolved:
                        self._add_resolved(expanded_ids, col, "concept_expand", 0.6 * e.confidence)

            elif node.type == NodeType.ENTITY:
                # Entity → concepts → columns
                entity_edges = self.storage.get_edges_for_node(node.id, EdgeType.HAS_CONCEPT)
                for e in entity_edges:
                    concept_id = e.target_id if e.source_id == node.id else e.source_id
                    concept = self.storage.get_node(concept_id)
                    if concept and concept.id not in resolved:
                        self._add_resolved(
                            expanded_ids,
                            concept,
                            "entity_expand",
                            0.5 * e.confidence,
                        )
                    if concept:
                        rep_edges = self.storage.get_edges_for_node(concept_id, EdgeType.REPRESENTS)
                        for re_ in rep_edges:
                            col_id = re_.source_id if re_.target_id == concept_id else re_.target_id
                            col = self.storage.get_node(col_id)
                            if col and col.id not in resolved:
                                self._add_resolved(
                                    expanded_ids,
                                    col,
                                    "entity_concept_expand",
                                    0.4 * e.confidence * re_.confidence,
                                )

            elif node.type == NodeType.TABLE:
                # Table → all contained columns
                contains_edges = self.storage.get_edges_for_node(node.id, EdgeType.CONTAINS)
                for e in contains_edges:
                    col_id = e.target_id  # contains: table → column
                    col = self.storage.get_node(col_id)
                    if col and col.id not in resolved:
                        self._add_resolved(expanded_ids, col, "table_expand", 0.4)

            elif node.type == NodeType.COLUMN:
                # Column → related columns via FK/joinable/semantic_synonym
                for et in (EdgeType.FOREIGN_KEY, EdgeType.JOINABLE, EdgeType.SEMANTIC_SYNONYM):
                    col_edges = self.storage.get_edges_for_node(node.id, et)
                    for e_ in col_edges:
                        other_id = e_.target_id if e_.source_id == node.id else e_.source_id
                        other = self.storage.get_node(other_id)
                        if other and other.id not in resolved:
                            self._add_resolved(
                                expanded_ids,
                                other,
                                "column_expand",
                                0.3 * e_.confidence,
                            )

        # Merge expanded into resolved (expanded nodes are lower priority)
        for nid, rn in expanded_ids.items():
            if nid not in resolved:
                resolved[nid] = rn

        # Sort by relevance_score (descending), truncate to limit
        sorted_resolved = sorted(resolved.values(), key=lambda r: r.relevance_score, reverse=True)
        return sorted_resolved[:limit]

    def _add_resolved(self, resolved: dict[str, ResolvedNode], node: Node, reason: str, score: float) -> None:
        """Add a node to the resolved dict, keeping the best score if already present."""
        if node.id in resolved:
            existing = resolved[node.id]
            if score > existing.relevance_score:
                resolved[node.id] = ResolvedNode(
                    node_id=node.id,
                    name=node.name,
                    node_type=node.type,
                    match_reason=reason,
                    relevance_score=score,
                )
        else:
            resolved[node.id] = ResolvedNode(
                node_id=node.id,
                name=node.name,
                node_type=node.type,
                match_reason=reason,
                relevance_score=score,
            )

    def _build_context(self, resolved: list[ResolvedNode], budget: ExploreBudget) -> list[NodeContext]:
        """Build context for each resolved node: edges, profile, pending edges."""
        contexts = []
        for rn in resolved:
            node = self.storage.get_node(rn.node_id)
            if node is None:
                continue

            # Gather all edges, group by type category
            all_edges = self.storage.get_edges_for_node(node.id)
            edges_by_group: dict[str, list[dict[str, Any]]] = {}

            for edge in all_edges:
                other_id = edge.target_id if edge.source_id == node.id else edge.source_id
                other_node = self.storage.get_node(other_id)

                group_key = self._edge_group_key(edge.type)
                summary = {
                    "id": edge.id,
                    "type": edge.type.value,
                    "direction": "→" if edge.source_id == node.id else "←",
                    "other_id": other_id,
                    "other_name": other_node.name if other_node else "unknown",
                    "other_type": other_node.type.value if other_node else "unknown",
                    "confidence": edge.confidence,
                }

                if group_key not in edges_by_group:
                    edges_by_group[group_key] = []
                edges_by_group[group_key].append(summary)

            # Truncate each group to budget
            for key in edges_by_group:
                edges_by_group[key] = edges_by_group[key][: budget.max_edges_per_node]

            # Profile summary for column nodes
            profile_summary = None
            if node.type == NodeType.COLUMN:
                profile = self.storage.get_profile_for_column(node.id)
                if profile:
                    profile_summary = self._profile_summary(profile, budget)

            # Pending edges
            pending_edges_list = self.storage.get_pending_edges_for_node(node.id)
            pending_summaries = [
                {
                    "id": pe.id,
                    "type": pe.type.value,
                    "source_id": pe.source_id,
                    "target_id": pe.target_id,
                    "missing_endpoints": pe.missing_endpoints,
                    "note": _describe_missing(pe),
                    "confidence": pe.confidence,
                }
                for pe in pending_edges_list[:3]  # cap at 3 per node
            ]

            contexts.append(
                NodeContext(
                    node=node,
                    edges_by_group=edges_by_group,
                    profile_summary=profile_summary,
                    pending_edges=pending_summaries,
                )
            )

        return contexts

    def _edge_group_key(self, edge_type: EdgeType) -> str:
        """Map an EdgeType to a display group key."""
        if edge_type in JOIN_EDGE_TYPES or edge_type == EdgeType.FOREIGN_KEY:
            return "join"
        if edge_type in SEMANTIC_EDGE_TYPES:
            return "semantic"
        if edge_type in STATISTICAL_EDGE_TYPES:
            return "statistical"
        if edge_type == EdgeType.CONTAINS:
            return "contains"
        return "other"

    def _profile_summary(self, profile: ColumnProfile, budget: ExploreBudget) -> dict[str, Any]:
        """Extract key profile metrics, respecting budget for sample_values."""
        summary: dict[str, Any] = {
            "dtype": profile.dtype,
            "semantic_type": profile.semantic_type,
            "null_rate": profile.null_rate,
            "cardinality": profile.cardinality,
            "unique_rate": profile.unique_rate,
        }

        if budget.max_sample_values > 0:
            # Use top_values (distinct, frequency-ranked) instead of random
            # sample_values. Random samples often repeat the most frequent
            # value, producing output like "samples: [A, A, A, A, A]"
            # which is useless for agents.
            # Fallback: if top_values is empty, use sample_values.
            if profile.top_values:
                summary["representative_values"] = [
                    tv["value"] for tv in profile.top_values[: budget.max_sample_values]
                ]
            else:
                summary["representative_values"] = [str(v) for v in profile.sample_values[: budget.max_sample_values]]

        # Extra metrics for data_profile focus
        if budget.max_sample_values >= 10:
            summary["top_values"] = profile.top_values[:5]
            if profile.min_value is not None:
                summary["min_value"] = profile.min_value
                summary["max_value"] = profile.max_value
                summary["mean_value"] = profile.mean_value
            if profile.min_length is not None:
                summary["min_length"] = profile.min_length
                summary["max_length"] = profile.max_length

        return summary

    def _build_relationship_map(self, node_ids: set[str], budget: ExploreBudget) -> RelationshipMap:
        """Build the relationship network among resolved nodes.

        Direct connections: edges where both endpoints are in node_ids.
        Indirect connections: multi-hop paths between resolved nodes.
        """
        join_relations: list[dict[str, Any]] = []
        semantic_relations: list[dict[str, Any]] = []
        statistical_relations: list[dict[str, Any]] = []

        seen_edge_ids: set[str] = set()

        for nid in node_ids:
            edges = self.storage.get_edges_for_node(nid)
            for edge in edges:
                other_id = edge.target_id if edge.source_id == nid else edge.source_id
                if other_id not in node_ids:
                    continue
                # Dedup
                if edge.id in seen_edge_ids:
                    continue
                seen_edge_ids.add(edge.id)

                src_node = self.storage.get_node(edge.source_id)
                tgt_node = self.storage.get_node(edge.target_id)

                relation = {
                    "edge_id": edge.id,
                    "source_id": edge.source_id,
                    "source_name": src_node.name if src_node else "unknown",
                    "target_id": edge.target_id,
                    "target_name": tgt_node.name if tgt_node else "unknown",
                    "type": edge.type.value,
                    "confidence": edge.confidence,
                }

                if edge.type in JOIN_EDGE_TYPES or edge.type == EdgeType.FOREIGN_KEY:
                    join_relations.append(relation)
                elif edge.type in SEMANTIC_EDGE_TYPES:
                    semantic_relations.append(relation)
                elif edge.type in STATISTICAL_EDGE_TYPES:
                    statistical_relations.append(relation)

        # Indirect paths: only traverse JOIN-relevant edges (FK/joinable),
        # not structural edges (contains, represents, etc.) which produce
        # meaningless "column → table → column" chains.
        indirect_paths: list[dict[str, Any]] = []
        if budget.include_relationships:
            join_edge_types = [EdgeType.FOREIGN_KEY, EdgeType.JOINABLE]
            node_list = list(node_ids)
            max_pairs = min(len(node_list) * (len(node_list) - 1) // 2, 20)
            pair_count = 0
            for i, src_id in enumerate(node_list):
                for tgt_id in node_list[i + 1 :]:
                    if pair_count >= max_pairs:
                        break
                    # Use mask_credential=False here — masking is handled at
                    # the _format_output layer where we have full control.
                    paths = self.find_paths(
                        src_id,
                        tgt_id,
                        max_depth=3,
                        edge_types=join_edge_types,
                        mask_credential=False,
                    )
                    for p in paths[:2]:
                        # Only keep paths that cross table boundaries
                        src_node = self.storage.get_node(src_id)
                        tgt_node = self.storage.get_node(tgt_id)
                        if src_node and tgt_node:
                            src_table = self._table_name_for_col(src_id) if isinstance(src_node, ColumnNode) else None
                            tgt_table = self._table_name_for_col(tgt_id) if isinstance(tgt_node, ColumnNode) else None
                            if src_table and tgt_table and src_table != tgt_table:
                                indirect_paths.append(p)
                    pair_count += 1

        return RelationshipMap(
            join_relations=join_relations,
            semantic_relations=semantic_relations,
            statistical_relations=statistical_relations,
            indirect_paths=indirect_paths,
        )

    # ── New format helpers: meaning, also_related, data_line ───────────

    def _generate_meaning(self, ctx: NodeContext, budget: ExploreBudget) -> str | None:
        """Generate a meaning line for a column — combining comment, concept/entity
        absorption, and FK/joinable translation into one coherent description.

        Meaning parts are assembled in order:
        1. column comment (from SQL metadata or CSV header)
        2. concept absorption (represents/has_concept edges)
        3. FK/joinable translation (query-oriented)
        Parts are joined with "; ".
        """
        node = ctx.node
        if node.type != NodeType.COLUMN or not isinstance(node, ColumnNode):
            return None

        parts: list[str] = []

        # 1. Column comment
        comment = node.comment or node.properties.get("comment", "")
        if comment:
            parts.append(comment)

        # 2. Concept/entity absorption from semantic edges
        for e in ctx.edges_by_group.get("semantic", []):
            etype = e["type"]
            other_id = e["other_id"]
            other_node = self.storage.get_node(other_id)

            if etype == "represents" and other_node and isinstance(other_node, ConceptNode):
                # Column represents this concept — absorb its description/unit/dimension
                concept_parts = [f'represents concept "{other_node.name}"']
                desc_detail = []
                if other_node.description:
                    desc_detail.append(other_node.description)
                if other_node.unit:
                    desc_detail.append(f"unit: {other_node.unit}")
                if other_node.dimension:
                    desc_detail.append(f"dimension: {other_node.dimension}")
                if desc_detail:
                    concept_parts.append(f"({', '.join(desc_detail)})")
                parts.append(" ".join(concept_parts))

            elif etype == "has_concept" and other_node and isinstance(other_node, ConceptNode):
                # Entity → concept chain — absorb concept description
                concept_parts = [f'has concept "{other_node.name}"']
                desc_detail = []
                if other_node.description:
                    desc_detail.append(other_node.description)
                if other_node.unit:
                    desc_detail.append(f"unit: {other_node.unit}")
                if desc_detail:
                    concept_parts.append(f"({', '.join(desc_detail)})")
                parts.append(" ".join(concept_parts))

        # Also check: entity nodes connected via has_concept
        # An entity's has_concept edge: entity→concept. If this column is connected
        # to an entity, absorb entity description.
        for e in ctx.edges_by_group.get("semantic", []):
            etype = e["type"]
            other_id = e["other_id"]
            other_node = self.storage.get_node(other_id)

            if etype == "has_concept" and other_node and isinstance(other_node, EntityNode):
                entity_parts = [f'part of entity "{other_node.name}"']
                if other_node.description:
                    entity_parts.append(f"({other_node.description})")
                parts.append(" ".join(entity_parts))

        # 3. FK/joinable translation — query-oriented meaning
        for e in ctx.edges_by_group.get("join", []):
            etype = e["type"]
            direction = e["direction"]
            other_name = e["other_name"]
            other_id = e["other_id"]
            other_table = self._table_name_for_col(other_id) if e["other_type"] == "column" else None

            if etype == "foreign_key" and direction == "→":
                # Outbound FK: this column references another table's column
                dialect = self._dialect_for_node(node)
                src_table = self._table_name_for_col(node.id) or ""
                q_src = quote_identifier(src_table, dialect) if src_table else ""
                q_src_col = quote_identifier(node.name, dialect)
                if other_table:
                    q_tgt = quote_qualified(other_table, other_name, dialect=dialect)
                    tgt_label = f"{other_table}.{other_name}"
                else:
                    q_tgt = quote_identifier(other_name, dialect)
                    tgt_label = other_name
                parts.append(
                    f"foreign key referencing {tgt_label} — "
                    f"use `JOIN {q_src} ON {q_src}.{q_src_col} = {q_tgt}` "
                    f"to link {src_table} with {other_table or other_name}"
                )
            elif etype == "foreign_key" and direction == "←":
                # Inbound FK: another table's column references this one
                src_label = f"{other_table}.{other_name}" if other_table else other_name
                parts.append(
                    f"primary key, referenced by {src_label} — target of JOIN from {other_table or other_name}"
                )
            elif etype == "joinable":
                tgt_label = f"{other_table}.{other_name}" if other_table else other_name
                parts.append(f"joinable with {tgt_label} — can JOIN on these columns")

        if not parts:
            return None

        return "; ".join(parts)

    def _generate_also_related(self, ctx: NodeContext, budget: ExploreBudget) -> str | None:
        """Generate the 'also related' line — synonym/statistical relationships
        not in meaning, translated into compact human-readable descriptions."""
        items: list[str] = []

        for e in ctx.edges_by_group.get("semantic", []):
            etype = e["type"]
            other_name = e["other_name"]
            other_id = e["other_id"]
            other_type = e["other_type"]
            conf = e["confidence"]

            # Skip represents and has_concept — those are in meaning
            if etype in ("represents", "has_concept"):
                continue

            other_table = self._table_name_for_col(other_id) if other_type == "column" else None
            other_label = f"{other_table}.{other_name}" if other_table else other_name

            if etype == "semantic_synonym":
                items.append(f"synonym↔{other_label} ({conf:.2f}) — same semantic meaning")
            elif etype == "semantic_type_match":
                items.append(f"semantic_match↔{other_label} ({conf:.2f})")

        for e in ctx.edges_by_group.get("statistical", []):
            etype = e["type"]
            other_name = e["other_name"]
            other_id = e["other_id"]
            other_type = e["other_type"]
            conf = e["confidence"]

            other_table = self._table_name_for_col(other_id) if other_type == "column" else None
            other_label = f"{other_table}.{other_name}" if other_table else other_name

            if etype == "correlated":
                items.append(f"correlated↔{other_label} ({conf:.2f}) — values tend to move together")
            elif etype == "distribution_similar":
                items.append(f"distribution_similar↔{other_label} ({conf:.2f})")

        if not items:
            return None

        return "; ".join(items[: budget.max_edges_per_node])

    def _format_data_line(self, ctx: NodeContext, budget: ExploreBudget) -> str | None:
        """Format the data profile as a compact single line."""
        if ctx.profile_summary is None:
            return None

        p = ctx.profile_summary
        bits: list[str] = []

        null_pct = f"{p['null_rate'] * 100:.0f}%"
        bits.append(f"null {null_pct}")
        bits.append(f"unique {p['unique_rate'] * 100:.0f}%")

        # Numeric range
        if "min_value" in p and p.get("min_value") is not None:
            bits.append(f"range {p['min_value']}–{p['max_value']}")

        # Representative values (distinct, from top_values)
        if budget.max_sample_values > 0 and "representative_values" in p and p["representative_values"]:
            vals = [str(v) for v in p["representative_values"][: budget.max_sample_values]]
            bits.append(f"top values: [{', '.join(vals)}]")

        return ", ".join(bits)

    def _format_cross_table_paths(
        self,
        contexts: list[NodeContext],
        rel_map: RelationshipMap,
        budget: ExploreBudget,
        mask_credential: bool = True,
    ) -> list[str]:
        """Format cross-table query patterns — FK/joinable connections between
        different tables, with SQL JOIN templates.

        This replaces the old Relationship section's join/semantic/statistical
        details (now inlined into meaning + also related). We only show
        cross-table connections here as a navigable summary.
        """
        lines: list[str] = []

        # Collect cross-table FK/joinable edges from contexts
        # (direct FK/joinable where the two columns belong to different tables)
        cross_table_edges: list[dict[str, Any]] = []
        seen_edge_ids: set[str] = set()

        for ctx in contexts:
            if ctx.node.type != NodeType.COLUMN:
                continue
            for e in ctx.edges_by_group.get("join", []):
                if e["id"] in seen_edge_ids:
                    continue
                seen_edge_ids.add(e["id"])

                etype = e["type"]
                other_id = e["other_id"]

                # Only cross-table connections
                src_table = self._table_name_for_col(ctx.node.id)
                tgt_table = self._table_name_for_col(other_id) if e["other_type"] == "column" else None

                if src_table and tgt_table and src_table != tgt_table:
                    src_label = f"{src_table}.{ctx.node.name}"
                    tgt_label = f"{tgt_table}.{e['other_name']}"
                    label = "FK" if etype == "foreign_key" else "joinable"
                    arrow = "→" if etype == "foreign_key" else "↔"
                    direction = e["direction"]
                    dialect = self._dialect_for_node(ctx.node)

                    if direction == "←":
                        # Flip presentation: other → this
                        src_label, tgt_label = tgt_label, src_label

                    cross_table_edges.append(
                        {
                            "src_label": src_label,
                            "tgt_label": tgt_label,
                            "arrow": arrow,
                            "label": label,
                            "confidence": e["confidence"],
                            "src_table": src_table if direction == "→" else tgt_table,
                            "tgt_table": tgt_table if direction == "→" else src_table,
                            "src_col": ctx.node.name if direction == "→" else e["other_name"],
                            "tgt_col": e["other_name"] if direction == "→" else ctx.node.name,
                            "dialect": dialect,
                        }
                    )

        # Deduplicate (both directions of same edge)
        deduped: list[dict[str, Any]] = []
        seen_pairs: set[str] = set()
        for edge in cross_table_edges:
            pair_key = f"{edge['src_label']}|{edge['tgt_label']}"
            reverse_key = f"{edge['tgt_label']}|{edge['src_label']}"
            if pair_key not in seen_pairs and reverse_key not in seen_pairs:
                seen_pairs.add(pair_key)
                deduped.append(edge)

        if not deduped:
            return lines

        lines.append("")
        lines.append("## Cross-table query patterns")

        for edge in deduped[: budget.max_edges_per_relationship_kind]:
            conf = edge["confidence"]
            conf_note = " (low conf)" if conf < 0.5 else ""
            lines.append(
                f"{edge['src_label']} {edge['arrow']} {edge['tgt_label']} ({edge['label']}, conf {conf:.2f}){conf_note}"
            )
            # Generate SQL template for FK/joinable (identifiers quoted for
            # hyphenated / reserved names — dialect inferred from source URL).
            dialect = edge.get("dialect")
            q_src_t = quote_identifier(edge["src_table"], dialect)
            q_tgt_t = quote_identifier(edge["tgt_table"], dialect)
            q_src_c = quote_identifier(edge["src_col"], dialect)
            q_tgt_c = quote_identifier(edge["tgt_col"], dialect)
            sql = f"SELECT * FROM {q_src_t} JOIN {q_tgt_t} ON {q_src_t}.{q_src_c} = {q_tgt_t}.{q_tgt_c}"
            lines.append(f"  SQL: {sql}")

        # Multi-hop indirect paths (only cross-table JOIN paths)
        if rel_map.indirect_paths:
            lines.append("")
            lines.append("Indirect paths:")
            for p in rel_map.indirect_paths[:5]:
                path_parts = []
                for nid in p["nodes"]:
                    node = self.storage.get_node(nid)
                    if node and isinstance(node, ColumnNode):
                        tname = self._table_name_for_col(node.id)
                        path_parts.append(f"{tname}.{node.name}" if tname else node.name)
                    else:
                        # Skip non-column nodes in path display (tables, concepts)
                        # They don't have meaningful table.column format
                        path_parts.append(node.name if node else nid)
                path_desc = " → ".join(path_parts)
                conf_note = " (low conf)" if p["confidence"] < 0.5 else ""
                lines.append(f"  {path_desc}  [indirect, {p['length']} hops, conf {p['confidence']:.2f}]{conf_note}")

        return lines

    # Low-confidence sentinel (shared between emitter and detector)
    LOW_CONFIDENCE_MARKER = "### ⚠️ Low-confidence match"

    def _format_output(
        self,
        contexts: list[NodeContext],
        rel_map: RelationshipMap,
        resolved: list[ResolvedNode],
        budget: ExploreBudget,
        mask_credential: bool = True,
    ) -> str:
        """Format the explore response as self-contained, entity-aggregated text.

        Each column gets its own complete context (meaning + data + also related).
        Concept/Entity nodes are absorbed into column meaning lines, not shown
        independently. Cross-table query patterns are shown as a summary section.
        """
        # Filter out concept/entity contexts — their info is absorbed into
        # column meaning lines via _generate_meaning()
        filtered_contexts = [ctx for ctx in contexts if ctx.node.type in (NodeType.TABLE, NodeType.COLUMN)]

        # Low-confidence check
        best_score = max(rn.relevance_score for rn in resolved) if resolved else 0.0
        if budget.include_low_confidence_marker and best_score < 0.5:
            lines = [self.LOW_CONFIDENCE_MARKER]
            lines.append("Results are approximate — try different keywords.")
        else:
            lines = []

        # Summary header — only show scope info (what was found),
        # not a fake "query" reconstructed from node names
        dataset_count = self.storage.count_nodes(NodeType.TABLE)
        col_count = len([c for c in filtered_contexts if c.node.type == NodeType.COLUMN])
        lines.append(f"matched: {col_count} columns across {dataset_count} tables")

        # Group by table (concept/entity already filtered out)
        table_groups = self._group_by_table(filtered_contexts)

        for group in table_groups:
            table = group.table_node
            table_name = table.name
            if isinstance(table, TableNode):
                row_count = table.properties.get("row_count", 0)
                source = table.properties.get("source", "")
                table_comment = table.properties.get("comment", "")
            else:
                row_count = 0
                source = ""
                table_comment = ""

            # Truncate columns to budget
            shown_columns = group.column_nodes[: budget.max_columns_per_table]
            matched_columns = len(group.column_nodes)  # columns matched by this query
            # Total columns in the table (from TableNode metadata, not just matched ones)
            table_total_columns = 0
            if isinstance(table, TableNode) and table.column_ids:
                table_total_columns = len(table.column_ids)
            elif isinstance(table, TableNode):
                # Fallback: count from storage if column_ids not populated
                table_total_columns = len(self.storage.get_edges_for_node(table.id, EdgeType.CONTAINS))

            suffix_parts = []
            if table_total_columns > 0 and matched_columns != table_total_columns:
                # Not all columns matched — show both matched count and total
                shown_count = len(shown_columns)
                if shown_count < matched_columns:
                    # Budget truncated even the matched set
                    suffix_parts.append(
                        f"{shown_count}/{matched_columns} matched columns (of {table_total_columns} total)"
                    )
                else:
                    suffix_parts.append(f"{matched_columns} matched columns (of {table_total_columns} total)")
            else:
                # All columns matched (or only table-level info without column breakdown)
                shown_count = len(shown_columns)
                if table_total_columns > 0:
                    if shown_count < table_total_columns:
                        suffix_parts.append(f"{shown_count}/{table_total_columns} columns")
                    else:
                        suffix_parts.append(f"{table_total_columns} columns")
                else:
                    suffix_parts.append(f"{matched_columns} columns")
            if row_count:
                suffix_parts.append(f"{row_count:,} rows")
            if source:
                display_source = mask_credentials(source) if mask_credential else source
                suffix_parts.append(f"source: {display_source}")
            suffix = ", ".join(suffix_parts)

            lines.append("")
            lines.append(f"## {table_name} — {suffix}")

            # Table meaning line: just the table comment (if available)
            if table_comment:
                lines.append(f"- meaning: {table_comment}")

            for ctx in shown_columns:
                lines.extend(self._format_column_selfcontained(ctx, budget))

            if matched_columns > budget.max_columns_per_table:
                omitted = matched_columns - budget.max_columns_per_table
                lines.append(f"  …and {omitted} more columns")

        # Cross-table query patterns
        cross_paths = self._format_cross_table_paths(filtered_contexts, rel_map, budget, mask_credential)
        lines.extend(cross_paths)

        # Completeness signal — simple one-liner
        if budget.include_completeness_signal and dataset_count < 3:
            lines.append("")
            lines.append("All indexed data included — no further explore calls needed.")

        # Final truncation
        text = "\n".join(lines)
        if len(text) > budget.max_output_chars:
            cut_text = text[: budget.max_output_chars]
            last_section = cut_text.rfind("\n## ")
            if last_section > budget.max_output_chars * 0.5:
                text = cut_text[:last_section]
            else:
                text = cut_text

        return text

    def _table_name_for_col(self, col_id: str) -> str | None:
        """Get the parent table name for a column node ID."""
        col = self.storage.get_node(col_id)
        if col and isinstance(col, ColumnNode):
            table = self.storage.get_node(col.table_id)
            if table:
                return table.name
        # Fallback: parse from column ID (source may contain colons)
        parsed = parse_column_id(col_id)
        return parsed.table_name if parsed else None

    def _dialect_for_node(self, node: Node) -> str | None:
        """Infer SQL dialect from a node's ``source`` field when present."""
        source = getattr(node, "source", None) or ""
        if not source and isinstance(node, ColumnNode):
            table = self.storage.get_node(node.table_id)
            if table:
                source = getattr(table, "source", "") or ""
        return dialect_from_source(source)

    def _group_by_table(self, contexts: list[NodeContext]) -> list[TableGroup]:
        """Group NodeContexts by their parent table.

        Column nodes → grouped under their TableNode.
        Table nodes → their own group.
        Concept/Entity should be filtered out before calling this method
        (their info is absorbed into column meaning lines).
        """
        table_map: dict[str, TableGroup] = {}
        ungrouped_nodes: list[NodeContext] = []

        for ctx in contexts:
            node = ctx.node

            if node.type == NodeType.TABLE:
                if node.id not in table_map:
                    table_map[node.id] = TableGroup(table_node=node, column_nodes=[])

            elif node.type == NodeType.COLUMN and isinstance(node, ColumnNode):
                tid = node.table_id
                if tid not in table_map:
                    table_node = self.storage.get_node(tid)
                    if table_node:
                        table_map[tid] = TableGroup(table_node=table_node, column_nodes=[])
                    else:
                        ungrouped_nodes.append(ctx)
                        continue
                table_map[tid].column_nodes.append(ctx)

            elif node.type in (NodeType.CONCEPT, NodeType.ENTITY):
                ungrouped_nodes.append(ctx)

            else:
                ungrouped_nodes.append(ctx)

        groups = list(table_map.values())

        # Sort columns within each group: FK outbound first, then alphabetically
        for g in groups:
            g.column_nodes.sort(
                key=lambda c: (
                    0
                    if any(
                        e["type"] == "foreign_key" and e["direction"] == "→" for e in c.edges_by_group.get("join", [])
                    )
                    else 1,
                    c.node.name,
                )
            )

        if ungrouped_nodes:
            dummy = Node(id="", type=NodeType.TABLE, name="semantic_nodes")
            groups.append(
                TableGroup(
                    table_node=dummy,
                    column_nodes=ungrouped_nodes,
                    ungrouped=True,
                )
            )

        return groups

    def _format_column_selfcontained(self, ctx: NodeContext, budget: ExploreBudget) -> list[str]:
        """Format one column as self-contained: meaning + data + also related.

        Each column gets its own complete semantic context:
        - Line 1: ### col_name (dtype, semantic_type)
        - Line 2: - meaning: {comment + concept absorption + FK/joinable translation}
        - Line 3: - data: {null%, unique%, samples, range}
        - Line 4: - also related: {synonym/statistical relationships}
        - Line 5+: - pending edges (FK/joinable pointing to unresolved tables)
        """
        node = ctx.node
        lines: list[str] = []

        # Line 1: Column name + type tag (### heading)
        name = node.name
        if isinstance(node, ColumnNode):
            dtype = node.dtype or ""
            sem_type = node.semantic_type or ""
            type_tag = dtype
            if sem_type and sem_type != "unknown":
                type_tag = f"{dtype}, {sem_type}"
            lines.append(f"### {name} ({type_tag})")
        else:
            lines.append(f"### {name}")

        # Line 2: meaning
        meaning = self._generate_meaning(ctx, budget)
        if meaning:
            lines.append(f"- meaning: {meaning}")

        # Line 3: data profile
        data_line = self._format_data_line(ctx, budget)
        if data_line:
            lines.append(f"- data: {data_line}")

        # Line 4: also related
        also_related = self._generate_also_related(ctx, budget)
        if also_related:
            lines.append(f"- also related: {also_related}")

        # Line 5+: pending edges
        if ctx.pending_edges:
            for pe in ctx.pending_edges[: budget.max_pending_edges_per_node]:
                if pe["type"] in ("foreign_key", "joinable"):
                    lines.append(f"- pending {pe['type']} → ? ({pe['note']})")

        return lines

    def _node_name(self, node_id: str) -> str:
        """Get a node's name, or its ID if not found."""
        node = self.storage.get_node(node_id)
        return node.name if node else node_id

    def _short_id(self, node_id: str) -> str:
        """Convert a full node ID to a short alias for display.

        Returns:
          - Table: "frpm" (just the table name)
          - Column: "frpm.County Name" (table.column_name)
          - Concept: "free_meal_count" (just the concept name)
          - Entity: "school" (just the entity name)
          - Other: the name portion of the ID
        """
        node = self.storage.get_node(node_id)
        if node is None:
            # Fallback: parse from ID structure
            if node_id.startswith("column:"):
                parsed = parse_column_id(node_id)
                if parsed:
                    return f"{parsed.table_name}.{parsed.column_name}"
            return node_id

        if node.type == NodeType.COLUMN and isinstance(node, ColumnNode):
            table_name = self._table_name_for_col(node_id) or node.table_id.rsplit(":", 1)[-1]
            return f"{table_name}.{node.name}"
        elif node.type == NodeType.TABLE:
            return node.name
        else:
            return node.name

    # ── MCP text format methods ──────────────────────────────────────

    def format_search_nodes_text(self, results: list[dict[str, Any]], query: str, mask_credential: bool = True) -> str:
        """Format search_nodes results as compact natural language text for MCP."""
        if not results:
            return f'No results found for "{query}". Try different keywords or use datalink_explore.'

        lines = [f'Found {len(results)} nodes matching "{query}":']

        for node in results:
            ntype = node.get("type", "unknown")
            name = node.get("name", "")
            props = node.get("properties", {})
            full_id = node.get("id", "")

            if ntype == "column":
                dtype = props.get("dtype", "")
                sem_type = props.get("semantic_type", "")
                type_tag = dtype
                if sem_type and sem_type != "unknown":
                    type_tag = f"{dtype}, {sem_type}"
                table_name = self._table_name_for_col(full_id) or "?"
                desc = props.get("comment", "") or ""
                header = f"[column] {table_name}.{name} ({type_tag})"
                if desc:
                    header += f" — {desc}"

                # Edges summary as inline Related line
                edges_summary = node.get("edges_summary", [])
                related_parts = []
                for es in edges_summary[:3]:
                    es_type = es.get("type", "")
                    es_conf = es.get("confidence", 0)
                    if es_conf < 0.3:
                        continue
                    es_target_id = es.get("target_id", "")
                    target_short = self._short_id(es_target_id)
                    related_parts.append(f"{es_type}↔{target_short} ({es_conf:.2f})")

                lines.append(header)
                if related_parts:
                    lines.append(f"  Related: {', '.join(related_parts)}")

            elif ntype == "table":
                row_count = props.get("row_count", 0)
                comment = props.get("comment", "")
                header = f"[table] {name} ({row_count:,} rows)"
                if comment:
                    header += f" — {comment}"
                lines.append(header)

            elif ntype == "concept":
                desc = props.get("description", "")
                unit = props.get("unit", "")
                dim = props.get("dimension", "")
                header = f"[concept] {name}"
                detail_parts = []
                if desc:
                    detail_parts.append(desc)
                if unit:
                    detail_parts.append(f"unit: {unit}")
                if dim:
                    detail_parts.append(f"dim: {dim}")
                if detail_parts:
                    header += f" — {', '.join(detail_parts)}"
                lines.append(header)

            elif ntype == "entity":
                desc = props.get("description", "")
                header = f"[entity] {name}"
                if desc:
                    header += f" — {desc}"
                lines.append(header)

            else:
                lines.append(f"[{ntype}] {name}")

        return "\n".join(lines)

    def format_get_node_text(self, result: dict[str, Any], mask_credential: bool = True) -> str:
        """Format get_node result as compact natural language text for MCP."""
        ntype = result.get("type", "unknown")
        name = result.get("name", "")
        full_id = result.get("id", "")
        props = result.get("properties", {})

        lines = []

        if ntype == "table":
            row_count = props.get("row_count", 0)
            source = props.get("source", "")
            comment = props.get("comment", "")
            column_ids = props.get("column_ids", [])

            # Mask source if needed
            display_source = mask_credentials(source) if mask_credential else source

            header = f"table: {name} ({row_count:,} rows, {len(column_ids)} columns)"
            if display_source:
                header += f", source: {display_source}"
            lines.append(header)
            if comment:
                lines.append(f"  description: {comment}")

            # Column names (from properties.column_ids, extract just names)
            col_names = []
            for col_id in column_ids:
                col_node = self.storage.get_node(col_id)
                if col_node:
                    col_names.append(col_node.name)
                else:
                    # Parse from ID
                    parsed = parse_column_id(col_id)
                    col_names.append(parsed.column_name if parsed else col_id.rsplit(":", 1)[-1])

            lines.append(f"  Columns: {', '.join(col_names)}")

            # Non-structural relationships (edges, excluding contains which we already removed)
            edges = result.get("edges", [])
            if edges:
                lines.append("")
                lines.append("Relationships:")
                for e in edges[:10]:
                    e_type = e.get("type", "")
                    e_conf = e.get("confidence", 0)
                    direction = e.get("direction", "")
                    other_node = e.get("other_node") or {}
                    other_name = other_node.get("name", "?")
                    other_type = other_node.get("type", "")
                    other_id = other_node.get("id", "")

                    # Build short label
                    if other_type == "column":
                        other_short = self._short_id(other_id) if other_id else other_name
                    elif other_type == "table":
                        other_short = other_name
                    else:
                        other_short = other_name

                    arrow = "→" if direction == "outgoing" else "←"
                    label = "FK" if e_type == "foreign_key" else e_type

                    src_short = self._short_id(full_id) if ntype != "table" else ""
                    lines.append(f"  {name}.{src_short} {label}{arrow} {other_short} (conf {e_conf:.2f})")

            # Suggested/pending edges
            suggested = result.get("suggested_edges", [])
            if suggested:
                lines.append("")
                lines.append("Pending relationships:")
                for se in suggested[:5]:
                    se_type = se.get("type", "")
                    se_note = se.get("note", "")
                    se_conf = se.get("confidence", 0)
                    lines.append(f"  {se_type} → ? ({se_note}, conf {se_conf:.2f})")

        elif ntype == "column":
            dtype = props.get("dtype", "")
            sem_type = props.get("semantic_type", "")
            comment = props.get("comment", "")
            type_tag = dtype
            if sem_type and sem_type != "unknown":
                type_tag = f"{dtype}, {sem_type}"
            table_name = self._table_name_for_col(full_id) or "?"

            lines.append(f"column: {table_name}.{name} ({type_tag})")
            if comment:
                lines.append(f"  description: {comment}")

            # Profile info
            profile = result.get("profile")
            if profile:
                null_pct = f"{profile.get('null_rate', 0) * 100:.0f}%"
                unique_pct = f"{profile.get('unique_rate', 0) * 100:.0f}%"
                cardinality = profile.get("cardinality", "?")
                data_parts = [f"null {null_pct}", f"unique {unique_pct}", f"{cardinality} distinct values"]
                sample_vals = profile.get("sample_values", [])
                if sample_vals:
                    vals_str = ", ".join(str(v) for v in sample_vals[:5])
                    data_parts.append(f"sample values: [{vals_str}]")
                lines.append(f"  data: {', '.join(data_parts)}")

            # Relationship edges
            edges = result.get("edges", [])
            related_parts = []
            for e in edges[:8]:
                e_type = e.get("type", "")
                e_conf = e.get("confidence", 0)
                direction = e.get("direction", "")
                other_node = e.get("other_node") or {}
                other_name = other_node.get("name", "?")
                other_type = other_node.get("type", "")
                other_id = other_node.get("id", "")

                other_short = self._short_id(other_id) if other_id else other_name

                arrow = "→" if direction == "outgoing" else "←"
                label = "FK" if e_type == "foreign_key" else e_type

                related_parts.append(f"{label}{arrow}{other_short} ({e_conf:.2f})")

            if related_parts:
                lines.append(f"  Related: {', '.join(related_parts)}")

            # Pending edges
            suggested = result.get("suggested_edges", [])
            if suggested:
                for se in suggested[:3]:
                    se_type = se.get("type", "")
                    se_note = se.get("note", "")
                    se_conf = se.get("confidence", 0)
                    lines.append(f"  pending {se_type} → ? ({se_note}, conf {se_conf:.2f})")

        elif ntype in ("concept", "entity"):
            desc = props.get("description", "")
            lines.append(f"{ntype}: {name}")
            if desc:
                lines.append(f"  description: {desc}")

            edges = result.get("edges", [])
            if edges:
                related_parts = []
                for e in edges[:5]:
                    e_type = e.get("type", "")
                    e_conf = e.get("confidence", 0)
                    other_node = e.get("other_node") or {}
                    other_name = other_node.get("name", "?")
                    other_id = other_node.get("id", "")
                    other_short = self._short_id(other_id)
                    related_parts.append(f"{e_type}↔{other_short} ({e_conf:.2f})")
                if related_parts:
                    lines.append(f"  Related: {', '.join(related_parts)}")

        else:
            lines.append(f"{ntype}: {name}")

        return "\n".join(lines)

    def format_find_paths_text(
        self, paths: list[dict[str, Any]], source_id: str, target_id: str, mask_credential: bool = True
    ) -> str:
        """Format find_paths results as compact natural language text for MCP."""

        if not paths:
            short_src = self._short_id(source_id)
            short_tgt = self._short_id(target_id)
            return f"No paths found between {short_src} and {short_tgt}."

        lines = [f"Found {len(paths)} paths:"]

        for i, p in enumerate(paths):
            confidence = p.get("confidence", 0)
            length = p.get("length", 0)
            path_nodes = p.get("nodes", [])
            path_edges = p.get("edges", [])

            # Build short path description
            path_parts = []
            for nid in path_nodes:
                node = self.storage.get_node(nid)
                if node:
                    path_parts.append(self._short_id(nid))
                else:
                    path_parts.append(nid)

            # Interleave nodes with edge types
            desc_parts = []
            for j, node_label in enumerate(path_parts):
                desc_parts.append(node_label)
                if j < len(path_edges):
                    edge_type = path_edges[j].get("type", "?")
                    desc_parts.append(f"→ {edge_type} →")

            path_desc = " ".join(desc_parts)
            conf_note = " (low conf)" if confidence < 0.5 else ""
            lines.append(f"Path {i + 1} ({length} hops, conf {confidence:.2f}){conf_note}:")
            lines.append(f"  {path_desc}")

        return "\n".join(lines)

    def format_list_datasets_text(self, datasets: list[dict[str, Any]], mask_credential: bool = True) -> str:
        """Format list_datasets results as compact natural language text for MCP."""

        if not datasets:
            return "No datasets found. Use datalink add-table to add data sources."

        lines = [f"Datasets ({len(datasets)} tables):"]

        for ds in datasets:
            name = ds.get("name", "")
            row_count = ds.get("row_count", 0)
            col_count = ds.get("column_count", 0)
            inferred_edges = ds.get("inferred_edge_count", 0)
            pending_fk = ds.get("pending_fk_count", 0)

            line = f"  {name}: {row_count:,} rows, {col_count} cols, {inferred_edges} edges, {pending_fk} pending FK"
            lines.append(line)

        return "\n".join(lines)

    def format_list_pending_edges_text(self, results: list[dict[str, Any]], mask_credential: bool = True) -> str:
        """Format list_pending_edges results as compact natural language text for MCP."""

        if not results:
            return "No pending edges found — all edges are resolved."

        lines = [f"Pending edges ({len(results)} total):"]

        for r in results:
            pe_type = r.get("type", "")
            source_id = r.get("source_id", "")
            target_id = r.get("target_id", "")
            note = r.get("note", "")
            conf = r.get("confidence", 0)
            missing = r.get("missing_endpoints", [])

            src_short = self._short_id(source_id)
            tgt_short = self._short_id(target_id)

            # Show target as "?" if it's a missing endpoint
            if "target" in missing:
                tgt_display = "? (not yet in graph)"
            else:
                tgt_display = tgt_short

            lines.append(f"  [{pe_type}] {src_short} → {tgt_display} (conf {conf:.2f}, {note})")

        return "\n".join(lines)

    def format_extract_subgraph_text(self, result: dict[str, Any], mask_credential: bool = True) -> str:
        """Format extract_subgraph results as compact natural language text for MCP."""
        stats = result.get("stats", {})
        nodes = result.get("nodes", [])
        edges = result.get("edges", [])

        node_count = stats.get("node_count", len(nodes))
        edge_count = stats.get("edge_count", len(edges))
        hops = stats.get("hops", "?")

        lines = [f"Subgraph: {node_count} nodes, {edge_count} edges ({hops} hops from seed)"]

        # Nodes listing
        node_parts = []
        for n in nodes:
            ntype = n.get("type", "")
            name = n.get("name", "")
            full_id = n.get("id", "")
            props = n.get("properties", {})

            if ntype == "column":
                dtype = props.get("dtype", "")
                short = self._short_id(full_id)
                node_parts.append(f"[column] {short} ({dtype})")
            elif ntype == "table":
                node_parts.append(f"[table] {name}")
            else:
                node_parts.append(f"[{ntype}] {name}")

        lines.append(f"Nodes: {', '.join(node_parts)}")

        # Edges listing
        if edges:
            edge_parts = []
            for e in edges[:15]:
                e_type = e.get("type", "")
                src_id = e.get("source_id", "")
                tgt_id = e.get("target_id", "")
                src_short = self._short_id(src_id)
                tgt_short = self._short_id(tgt_id)
                edge_parts.append(f"{src_short} → {e_type} → {tgt_short}")
            lines.append(f"Edges: {', '.join(edge_parts)}")

        return "\n".join(lines)
