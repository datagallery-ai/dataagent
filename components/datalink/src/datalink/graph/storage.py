"""SQLite-based graph storage for DataLink."""

import json
import logging
import math
import sqlite3
import struct
from pathlib import Path
from typing import Any

from datalink.models.edge import Edge, EdgeType, PendingEdge
from datalink.models.node import ColumnNode, ConceptNode, EntityNode, Node, NodeType, TableNode
from datalink.models.profile import ColumnProfile

logger = logging.getLogger(__name__)

SCHEMA_SQL_PATH = Path(__file__).parent / "schema.sql"


class GraphStorage:
    """SQLite storage layer for the DataLink dual-layer graph.

    Provides CRUD operations for nodes, edges, and profiles,
    plus add_table/remove_table for incremental graph modifications.
    """

    def __init__(self, db_path: str = "datalink.db"):
        """Initialize or open the graph database.

        If db_path is a relative path or bare filename, it is resolved under
        ~/.datalink/storage/ (the directory is created automatically).
        If db_path is an absolute path, it is used as-is.

        Args:
            db_path: SQLite database path — filename, relative path, or absolute path.
        """
        self.db_path = self._resolve_db_path(db_path)
        self.conn: sqlite3.Connection | None = None
        self._open_and_init()

    @staticmethod
    def _resolve_db_path(db_path: str) -> str:
        """Resolve the database path.

        Absolute paths are used as-is. Relative paths and bare filenames
        are placed under ~/.datalink/storage/ (created if missing).
        """
        p = Path(db_path)
        if p.is_absolute():
            # Ensure parent directory exists
            p.parent.mkdir(parents=True, exist_ok=True)
            return str(p)
        # Relative or bare filename → ~/.datalink/storage/
        storage_dir = Path.home() / ".datalink" / "storage"
        storage_dir.mkdir(parents=True, exist_ok=True)
        return str(storage_dir / p)

    def _open_and_init(self) -> None:
        """Open the database connection and initialize the schema."""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")

        # Read and execute schema SQL
        with open(SCHEMA_SQL_PATH, encoding="utf-8") as f:
            schema_sql = f.read()
        self.conn.executescript(schema_sql)
        self.conn.commit()

        # Migrate old schema: remove ON DELETE CASCADE foreign keys from
        # edges and column_profiles tables. INSERT OR REPLACE on nodes
        # triggers an internal DELETE+INSERT that cascades and destroys
        # all referencing edges/profiles — which is catastrophic when
        # updating node properties. The new schema omits these FK
        # constraints, but IF NOT EXISTS skips the CREATE TABLE, so
        # we must explicitly migrate existing databases.
        self._migrate_remove_cascade_fks()

        logger.info(f"Graph storage initialized at '{self.db_path}'")

    def close(self) -> None:
        """Close the database connection."""
        if self.conn is not None:
            self.conn.close()
            self.conn = None
            logger.info("Graph storage connection closed")

    # --- Node operations ---

    def add_node(self, node: Node) -> None:
        """Add a node to the graph."""
        props = node.to_storage_properties()
        self.conn.execute(
            "INSERT OR REPLACE INTO nodes (id, type, name, properties) VALUES (?, ?, ?, ?)",
            (node.id, node.type.value, node.name, json.dumps(props)),
        )
        self.conn.commit()

    def get_node(self, node_id: str) -> Node | None:
        """Get a node by ID. Returns None if not found."""
        row = self.conn.execute(
            "SELECT id, type, name, properties FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_node(row)

    def get_nodes_by_type(self, node_type: NodeType) -> list[Node]:
        """Get all nodes of a specific type."""
        rows = self.conn.execute(
            "SELECT id, type, name, properties FROM nodes WHERE type = ?",
            (node_type.value,),
        ).fetchall()
        return [self._row_to_node(row) for row in rows]

    def search_nodes_by_name(self, query: str, node_type: NodeType | None = None, limit: int = 10) -> list[Node]:
        """Search nodes by name (substring match)."""
        if node_type:
            rows = self.conn.execute(
                "SELECT id, type, name, properties FROM nodes WHERE name LIKE ? AND type = ? LIMIT ?",
                (f"%{query}%", node_type.value, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, type, name, properties FROM nodes WHERE name LIKE ? LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        return [self._row_to_node(row) for row in rows]

    def remove_node(self, node_id: str) -> None:
        """Remove a node and all edges connected to it (cascade delete)."""
        # Count edges that will be removed
        edge_count = self.conn.execute(
            "SELECT COUNT(*) FROM edges WHERE source_id = ? OR target_id = ?",
            (node_id, node_id),
        ).fetchone()[0]

        # Remove edges and profiles explicitly (no FK cascade — see schema.sql notes)
        self.conn.execute("DELETE FROM edges WHERE source_id = ? OR target_id = ?", (node_id, node_id))
        self.conn.execute("DELETE FROM column_profiles WHERE column_id = ?", (node_id,))
        self.conn.execute("DELETE FROM node_embeddings WHERE node_id = ?", (node_id,))
        self.conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        self.conn.commit()
        logger.info(f"Removed node '{node_id}' and {edge_count} connected edges")

    def get_all_node_ids(self) -> list[str]:
        """Get all node IDs in the graph."""
        rows = self.conn.execute("SELECT id FROM nodes").fetchall()
        return [row[0] for row in rows]

    def count_nodes(self, node_type: NodeType | None = None) -> int:
        """Count nodes, optionally filtered by type."""
        if node_type:
            return self.conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE type = ?",
                (node_type.value,),
            ).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

    # --- Edge operations ---

    def add_edge(self, edge: Edge) -> None:
        """Add an edge to the graph."""
        self.conn.execute(
            "INSERT OR REPLACE INTO edges (id, source_id, target_id, type, confidence, properties) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (edge.id, edge.source_id, edge.target_id, edge.type.value, edge.confidence, json.dumps(edge.properties)),
        )
        self.conn.commit()

    def add_edges_batch(self, edges: list[Edge]) -> None:
        """Add multiple edges in a single transaction."""
        self.conn.executemany(
            "INSERT OR REPLACE INTO edges (id, source_id, target_id, type, confidence, properties) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(e.id, e.source_id, e.target_id, e.type.value, e.confidence, json.dumps(e.properties)) for e in edges],
        )
        self.conn.commit()

    def get_edge(self, edge_id: str) -> Edge | None:
        """Get an edge by ID."""
        row = self.conn.execute(
            "SELECT id, source_id, target_id, type, confidence, properties FROM edges WHERE id = ?",
            (edge_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_edge(row)

    def get_edges_for_node(self, node_id: str, edge_type: EdgeType | None = None) -> list[Edge]:
        """Get all edges connected to a node, optionally filtered by type."""
        if edge_type:
            rows = self.conn.execute(
                "SELECT id, source_id, target_id, type, confidence, properties FROM edges "
                "WHERE (source_id = ? OR target_id = ?) AND type = ?",
                (node_id, node_id, edge_type.value),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id, source_id, target_id, type, confidence, properties FROM edges "
                "WHERE source_id = ? OR target_id = ?",
                (node_id, node_id),
            ).fetchall()
        return [self._row_to_edge(row) for row in rows]

    def remove_edge(self, edge_id: str) -> None:
        """Remove a single edge."""
        self.conn.execute("DELETE FROM edges WHERE id = ?", (edge_id,))
        self.conn.commit()

    def remove_edges_by_types(self, edge_types: list[EdgeType]) -> int:
        """Remove all edges matching any of the given types.

        Used during profile rebuild to clear inferred edges that depend
        on profile statistics (joinable, distribution_similar, synonym, correlated)
        before re-computing them from updated profiles.

        Returns:
            Number of edges removed.
        """
        type_values = [t.value for t in edge_types]
        count = self.conn.execute(
            f"SELECT COUNT(*) FROM edges WHERE type IN ({','.join('?' for _ in type_values)})",
            type_values,
        ).fetchone()[0]
        self.conn.execute(
            f"DELETE FROM edges WHERE type IN ({','.join('?' for _ in type_values)})",
            type_values,
        )
        self.conn.commit()
        return count

    def count_edges(self, edge_type: EdgeType | None = None) -> int:
        """Count edges, optionally filtered by type."""
        if edge_type:
            return self.conn.execute(
                "SELECT COUNT(*) FROM edges WHERE type = ?",
                (edge_type.value,),
            ).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    # --- Pending edge operations ---
    # Pending edges: edges whose source/target node(s) don't yet exist in the graph.
    # They are stored separately from the main edges table and do not participate
    # in path traversal or subgraph expansion. When new nodes are added via add_table,
    # the Pipeline calls resolve_pending_edges() to move eligible pending edges into
    # the edges table.

    def add_pending_edge(self, edge: PendingEdge) -> None:
        """Add a pending edge to the graph."""
        self.conn.execute(
            "INSERT OR REPLACE INTO pending_edges "
            "(id, source_id, target_id, type, confidence, properties, missing_endpoints) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                edge.id,
                edge.source_id,
                edge.target_id,
                edge.type.value,
                edge.confidence,
                json.dumps(edge.properties),
                json.dumps(edge.missing_endpoints),
            ),
        )
        self.conn.commit()

    def add_pending_edges_batch(self, edges: list[PendingEdge]) -> None:
        """Add multiple pending edges in a single transaction."""
        self.conn.executemany(
            "INSERT OR REPLACE INTO pending_edges "
            "(id, source_id, target_id, type, confidence, properties, missing_endpoints) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    e.id,
                    e.source_id,
                    e.target_id,
                    e.type.value,
                    e.confidence,
                    json.dumps(e.properties),
                    json.dumps(e.missing_endpoints),
                )
                for e in edges
            ],
        )
        self.conn.commit()

    def get_pending_edge(self, edge_id: str) -> PendingEdge | None:
        """Get a pending edge by ID."""
        row = self.conn.execute(
            "SELECT id, source_id, target_id, type, confidence, properties, missing_endpoints "
            "FROM pending_edges WHERE id = ?",
            (edge_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_pending_edge(row)

    def get_pending_edges_for_node(self, node_id: str) -> list[PendingEdge]:
        """Get all pending edges involving a specific node."""
        rows = self.conn.execute(
            "SELECT id, source_id, target_id, type, confidence, properties, missing_endpoints "
            "FROM pending_edges WHERE source_id = ? OR target_id = ?",
            (node_id, node_id),
        ).fetchall()
        return [self._row_to_pending_edge(row) for row in rows]

    def get_all_pending_edges(self) -> list[PendingEdge]:
        """Get all pending edges."""
        rows = self.conn.execute(
            "SELECT id, source_id, target_id, type, confidence, properties, missing_endpoints FROM pending_edges"
        ).fetchall()
        return [self._row_to_pending_edge(row) for row in rows]

    def resolve_pending_edges(self, available_node_ids: set[str]) -> int:
        """Move pending edges whose both endpoints now exist into the edges table.

        Scans the pending_edges table and checks whether both source_id and
        target_id are present in available_node_ids. Eligible edges are moved
        to the edges table and removed from pending_edges.

        Args:
            available_node_ids: Set of node IDs that currently exist in the graph.

        Returns:
            Number of edges resolved and moved to the edges table.
        """
        rows = self.conn.execute(
            "SELECT id, source_id, target_id, type, confidence, properties, missing_endpoints FROM pending_edges"
        ).fetchall()

        resolved_count = 0
        for row in rows:
            id_, src, tgt, type_str, conf, props_json, missing_json = row
            if src in available_node_ids and tgt in available_node_ids:
                # Both endpoints exist → move to edges table
                self.conn.execute(
                    "INSERT OR REPLACE INTO edges "
                    "(id, source_id, target_id, type, confidence, properties) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (id_, src, tgt, type_str, conf, props_json),
                )
                self.conn.execute("DELETE FROM pending_edges WHERE id = ?", (id_,))
                resolved_count += 1

        self.conn.commit()
        if resolved_count:
            logger.info(f"Resolved {resolved_count} pending edges into the edges table")
        return resolved_count

    def remove_pending_edge(self, edge_id: str) -> None:
        """Remove a single pending edge."""
        self.conn.execute("DELETE FROM pending_edges WHERE id = ?", (edge_id,))
        self.conn.commit()

    def cleanup_pending_edges_for_removed_nodes(self, removed_node_ids: set[str]) -> int:
        """Remove pending edges that reference nodes that have been removed.

        If a referenced node no longer exists, the pending edge can never be
        resolved, so it should be cleaned up.

        Args:
            removed_node_ids: Set of node IDs that were removed from the graph.

        Returns:
            Number of pending edges removed.
        """
        removed_count = 0
        for nid in removed_node_ids:
            count = self.conn.execute(
                "SELECT COUNT(*) FROM pending_edges WHERE source_id = ? OR target_id = ?",
                (nid, nid),
            ).fetchone()[0]
            removed_count += count
            self.conn.execute(
                "DELETE FROM pending_edges WHERE source_id = ? OR target_id = ?",
                (nid, nid),
            )

        self.conn.commit()
        if removed_count:
            logger.info(f"Cleaned up {removed_count} pending edges referencing removed nodes")
        return removed_count

    def count_pending_edges(self, edge_type: EdgeType | None = None) -> int:
        """Count pending edges, optionally filtered by type."""
        if edge_type:
            return self.conn.execute(
                "SELECT COUNT(*) FROM pending_edges WHERE type = ?",
                (edge_type.value,),
            ).fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM pending_edges").fetchone()[0]

    # --- Profile operations ---

    def add_profile(self, profile: ColumnProfile) -> None:
        """Add a column profile."""
        props = profile.model_dump(
            exclude={"id", "column_id"},
            mode="json",
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO column_profiles (id, column_id, properties) VALUES (?, ?, ?)",
            (profile.id, profile.column_id, json.dumps(props)),
        )
        self.conn.commit()

    def add_profiles_batch(self, profiles: list[ColumnProfile]) -> None:
        """Add multiple profiles in a single transaction."""
        data = [
            (
                p.id,
                p.column_id,
                json.dumps(p.model_dump(exclude={"id", "column_id"}, mode="json")),
            )
            for p in profiles
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO column_profiles (id, column_id, properties) VALUES (?, ?, ?)",
            data,
        )
        self.conn.commit()

    def get_profile(self, profile_id: str) -> ColumnProfile | None:
        """Get a column profile by ID."""
        row = self.conn.execute(
            "SELECT id, column_id, properties FROM column_profiles WHERE id = ?",
            (profile_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_profile(row)

    def get_profile_for_column(self, column_id: str) -> ColumnProfile | None:
        """Get the profile for a specific column."""
        row = self.conn.execute(
            "SELECT id, column_id, properties FROM column_profiles WHERE column_id = ?",
            (column_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_profile(row)

    # --- Bulk operations for add_table/remove_table ---

    def add_nodes_batch(self, nodes: list[Node]) -> None:
        """Add multiple nodes in a single transaction."""
        self.conn.executemany(
            "INSERT OR REPLACE INTO nodes (id, type, name, properties) VALUES (?, ?, ?, ?)",
            [(n.id, n.type.value, n.name, json.dumps(n.to_storage_properties())) for n in nodes],
        )
        self.conn.commit()

    def remove_table(self, table_id: str) -> list[str]:
        """Remove a table and all its columns, edges, profiles.

        Returns list of removed column IDs for cleanup of semantic nodes.
        """
        # Find all columns in this table via contains edges
        contains_edges = self.get_edges_for_node(table_id, EdgeType.CONTAINS)
        column_ids = [e.target_id for e in contains_edges]

        # Remove all edges touching the table or its columns
        all_ids = [table_id] + column_ids
        for nid in all_ids:
            self.conn.execute("DELETE FROM edges WHERE source_id = ? OR target_id = ?", (nid, nid))

        # Remove all profiles for these columns
        for col_id in column_ids:
            self.conn.execute("DELETE FROM column_profiles WHERE column_id = ?", (col_id,))

        # Remove all embeddings for removed nodes
        for nid in all_ids:
            self.conn.execute("DELETE FROM node_embeddings WHERE node_id = ?", (nid,))

        # Remove column nodes and table node
        for col_id in column_ids:
            self.conn.execute("DELETE FROM nodes WHERE id = ?", (col_id,))
        self.conn.execute("DELETE FROM nodes WHERE id = ?", (table_id,))

        self.conn.commit()
        logger.info(f"Removed table '{table_id}' with {len(column_ids)} columns")
        return column_ids

    def cleanup_orphaned_semantic_nodes(self) -> int:
        """Remove Concept/Entity nodes that have no remaining connection
        to the structural layer, in two stages:

        Stage 1 — Concept cleanup:
            A Concept is anchored when at least one structural node
            (Column/Table) has a ``represents`` edge targeting it.
            Unanchored Concepts are deleted along with all edges
            touching them (including ``has_concept`` edges from Entities).

        Stage 2 — Entity cleanup:
            An Entity is anchored when it has ``has_concept`` edges
            targeting **surviving** Concepts (those kept in Stage 1),
            OR when it has outgoing edges to structural nodes.
            Unanchored Entities are deleted along with all edges
            touching them.

        ``has_concept`` edges between two semantic-layer nodes (Entity → Concept)
        do NOT directly anchor a Concept — they are semantic-layer-internal.
        But they DO anchor an Entity indirectly: if the target Concept survives
        Stage 1 (is anchored by the structural layer), the Entity is also anchored.

        Returns the count of removed nodes.
        """
        structural_types = (NodeType.COLUMN.value, NodeType.TABLE.value)
        orphan_count = 0

        # ── Stage 1: Concept cleanup ──────────────────────────────────
        # Find Concepts anchored by structural represents edges
        anchored_concept_ids: set[str] = set()
        anchored_rows = self.conn.execute(
            "SELECT target_id FROM edges WHERE type = ? AND source_id IN (SELECT id FROM nodes WHERE type IN (?, ?))",
            (EdgeType.REPRESENTS.value, *structural_types),
        ).fetchall()
        anchored_concept_ids = {row[0] for row in anchored_rows}

        # Remove unanchored Concepts and all their edges
        concept_rows = self.conn.execute(
            "SELECT id FROM nodes WHERE type = ?",
            (NodeType.CONCEPT.value,),
        ).fetchall()
        for row in concept_rows:
            node_id = row[0]
            if node_id not in anchored_concept_ids:
                self.conn.execute(
                    "DELETE FROM edges WHERE source_id = ? OR target_id = ?",
                    (node_id, node_id),
                )
                self.conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
                orphan_count += 1

        # ── Stage 2: Entity cleanup ───────────────────────────────────
        # Re-query anchored concepts (unchanged set — we only deleted
        # unanchored ones, anchored ones are still in the DB)
        # An Entity is anchored if it has has_concept edges to surviving
        # Concepts, OR outgoing edges to structural nodes.
        entity_rows = self.conn.execute(
            "SELECT id FROM nodes WHERE type = ?",
            (NodeType.ENTITY.value,),
        ).fetchall()
        for row in entity_rows:
            node_id = row[0]

            # Direct structural anchor
            outgoing_to_structural = self.conn.execute(
                "SELECT COUNT(*) FROM edges "
                "WHERE source_id = ? "
                "AND target_id IN (SELECT id FROM nodes WHERE type IN (?, ?))",
                (node_id, *structural_types),
            ).fetchone()[0]

            # Indirect anchor through surviving Concepts
            has_concept_to_anchored = 0
            if anchored_concept_ids:
                placeholders = ",".join("?" for _ in anchored_concept_ids)
                has_concept_to_anchored = self.conn.execute(
                    "SELECT COUNT(*) FROM edges "
                    "WHERE source_id = ? AND type = ? "
                    "AND target_id IN (" + placeholders + ")",
                    (node_id, EdgeType.HAS_CONCEPT.value, *anchored_concept_ids),
                ).fetchone()[0]

            if outgoing_to_structural == 0 and has_concept_to_anchored == 0:
                self.conn.execute(
                    "DELETE FROM edges WHERE source_id = ? OR target_id = ?",
                    (node_id, node_id),
                )
                self.conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
                orphan_count += 1

        self.conn.commit()
        logger.info(f"Cleaned up {orphan_count} orphaned semantic nodes")
        return orphan_count

    # --- Metadata operations ---

    def set_metadata(self, key: str, value: str) -> None:
        """Set a metadata key-value pair."""
        self.conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, value),
        )
        self.conn.commit()

    def get_metadata(self, key: str) -> str | None:
        """Get a metadata value by key."""
        row = self.conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    # --- Embedding operations ---

    def add_embedding(
        self,
        node_id: str,
        embedding_model: str,
        embedding_vector: list[float],
        searchable_text: str,
    ) -> None:
        """Store an embedding vector for a node.

        The float32 vector is serialized as a BLOB for compact storage
        (4 bytes per dimension, vs ~20 bytes per dimension as JSON text).
        """
        vector_blob = struct.pack(f"<{len(embedding_vector)}f", *embedding_vector)
        self.conn.execute(
            "INSERT OR REPLACE INTO node_embeddings (node_id, embedding_model, embedding_vector, searchable_text) "
            "VALUES (?, ?, ?, ?)",
            (node_id, embedding_model, vector_blob, searchable_text),
        )
        self.conn.commit()

    def add_embeddings_batch(
        self,
        data: list[tuple[str, str, list[float], str]],
    ) -> None:
        """Add multiple embeddings in a single transaction.

        Args:
            data: List of (node_id, embedding_model, embedding_vector, searchable_text) tuples.
        """
        rows = [
            (
                node_id,
                embedding_model,
                struct.pack(f"<{len(vec)}f", *vec),
                searchable_text,
            )
            for node_id, embedding_model, vec, searchable_text in data
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO node_embeddings (node_id, embedding_model, embedding_vector, searchable_text) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()

    def get_embedding(self, node_id: str) -> dict[str, Any] | None:
        """Get an embedding for a node.

        Returns dict with node_id, embedding_model, embedding_vector (list[float]),
        searchable_text, or None if not found.
        """
        row = self.conn.execute(
            "SELECT node_id, embedding_model, embedding_vector, searchable_text FROM node_embeddings WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        node_id, model, blob, text = row
        dim = len(blob) // 4
        vector = list(struct.unpack(f"<{dim}f", blob))
        return {
            "node_id": node_id,
            "embedding_model": model,
            "embedding_vector": vector,
            "searchable_text": text,
        }

    def get_all_embeddings(self, node_type: NodeType | None = None) -> list[dict[str, Any]]:
        """Get all embeddings, optionally filtered by node type.

        Returns list of dicts with node_id, embedding_model, embedding_vector (list[float]),
        searchable_text, and node_type (from the nodes table).
        """
        if node_type:
            rows = self.conn.execute(
                "SELECT ne.node_id, ne.embedding_model, ne.embedding_vector, ne.searchable_text, n.type "
                "FROM node_embeddings ne JOIN nodes n ON ne.node_id = n.id "
                "WHERE n.type = ?",
                (node_type.value,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT ne.node_id, ne.embedding_model, ne.embedding_vector, ne.searchable_text, n.type "
                "FROM node_embeddings ne JOIN nodes n ON ne.node_id = n.id"
            ).fetchall()

        result = []
        for row in rows:
            nid, model, blob, text, nt = row
            dim = len(blob) // 4
            vector = list(struct.unpack(f"<{dim}f", blob))
            result.append(
                {
                    "node_id": nid,
                    "embedding_model": model,
                    "embedding_vector": vector,
                    "searchable_text": text,
                    "node_type": nt,
                }
            )
        return result

    def clear_embeddings(self) -> None:
        """Delete all embedding vectors (keep schema)."""
        self.conn.execute("DELETE FROM node_embeddings")
        self.conn.commit()
        logger.info("All embedding vectors cleared")

    def get_embedding_model(self) -> str | None:
        """Get the stored embedding model name from metadata.

        Returns None if no embedding model has been recorded.
        """
        return self.get_metadata("embedding_model")

    def search_nodes_by_embedding(
        self,
        query_vector: list[float],
        node_type: NodeType | None = None,
        limit: int = 10,
        threshold: float = 0.5,
    ) -> list[tuple[Node, float]]:
        """Search nodes by embedding vector similarity (cosine similarity).

        Loads all embeddings from DB, computes cosine similarity in Python,
        filters by threshold, and returns (node, similarity) pairs sorted
        by similarity descending.

        Args:
            query_vector: The query embedding vector.
            node_type: Optional filter by node type.
            limit: Maximum number of results.
            threshold: Minimum cosine similarity to include.

        Returns:
            List of (Node, similarity_score) tuples, sorted by similarity descending.
        """
        embeddings = self.get_all_embeddings(node_type)

        if not embeddings:
            return []

        results: list[tuple[Node, float]] = []
        query_norm = math.sqrt(sum(v * v for v in query_vector))
        if query_norm == 0.0:
            return []

        for emb in embeddings:
            vec = emb["embedding_vector"]
            vec_norm = math.sqrt(sum(v * v for v in vec))
            if vec_norm == 0.0:
                continue

            dot = sum(a * b for a, b in zip(query_vector, vec))
            sim = dot / (query_norm * vec_norm)

            if sim >= threshold:
                node = self.get_node(emb["node_id"])
                if node:
                    results.append((node, sim))

        # Sort by similarity descending
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]

    # --- Utility ---

    def get_graph_stats(self) -> dict[str, Any]:
        """Get overview statistics of the graph."""
        node_counts = {}
        for nt in NodeType:
            node_counts[nt.value] = self.count_nodes(nt)

        edge_counts = {}
        for et in EdgeType:
            edge_counts[et.value] = self.count_edges(et)

        pending_counts = {}
        for et in EdgeType:
            pending_counts[et.value] = self.count_pending_edges(et)

        return {
            "total_nodes": self.count_nodes(),
            "node_type_counts": node_counts,
            "total_edges": self.count_edges(),
            "edge_type_counts": edge_counts,
            "total_profiles": self.conn.execute("SELECT COUNT(*) FROM column_profiles").fetchone()[0],
            "pending_edge_count": self.count_pending_edges(),
            "pending_edge_type_counts": pending_counts,
        }

    def show_graph(self) -> dict[str, Any]:
        """Return all nodes and edges as a structured dict for the show command.

        Returns:
            {"nodes": [{"id", "type", "name", "properties"}, ...],
             "edges": [{"id", "source_id", "target_id", "type", "confidence", "properties"}, ...]}
        """
        node_rows = self.conn.execute("SELECT id, type, name, properties FROM nodes").fetchall()
        nodes = []
        for row in node_rows:
            id_str, type_str, name, properties_json = row
            nodes.append(
                {
                    "id": id_str,
                    "type": type_str,
                    "name": name,
                    "properties": json.loads(properties_json) if properties_json else {},
                }
            )

        edge_rows = self.conn.execute(
            "SELECT id, source_id, target_id, type, confidence, properties FROM edges"
        ).fetchall()
        edges = []
        for row in edge_rows:
            id_str, source_id, target_id, type_str, confidence, properties_json = row
            edges.append(
                {
                    "id": id_str,
                    "source_id": source_id,
                    "target_id": target_id,
                    "type": type_str,
                    "confidence": confidence,
                    "properties": json.loads(properties_json) if properties_json else {},
                }
            )

        return {"nodes": nodes, "edges": edges}

    def clear_all(self) -> None:
        """Delete all data from the graph (keep schema)."""
        self.conn.execute("DELETE FROM edges")
        self.conn.execute("DELETE FROM pending_edges")
        self.conn.execute("DELETE FROM column_profiles")
        self.conn.execute("DELETE FROM node_embeddings")
        self.conn.execute("DELETE FROM nodes")
        self.conn.execute("DELETE FROM metadata")
        self.conn.commit()
        logger.info("All graph data cleared")

    # --- Schema migration helpers ---

    def _migrate_remove_cascade_fks(self) -> None:
        """Remove ON DELETE CASCADE foreign keys from edges & column_profiles.

        SQLite does not support ALTER TABLE DROP CONSTRAINT, so we must
        recreate the tables. The strategy:

        1. Check if the old CASCADE FKs exist by probing pragma.
        2. If found, create temp tables → copy data → drop old → recreate
           without CASCADE → copy back → drop temp.
        3. This is safe for existing data and preserves all rows.
        """
        # Check edges table for cascade FKs on source_id / target_id
        needs_edges_migration = self._table_has_cascade_fk("edges", ["source_id", "target_id"])
        needs_profiles_migration = self._table_has_cascade_fk("column_profiles", ["column_id"])

        if not needs_edges_migration and not needs_profiles_migration:
            # Already migrated or fresh database — nothing to do
            return

        logger.info("Migrating schema: removing ON DELETE CASCADE foreign keys from edges/column_profiles")

        if needs_edges_migration:
            self._rebuild_table_without_cascade("edges")
            # Re-create indexes that were lost when the old table was dropped
            self.conn.executescript(
                "CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);"
                "CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);"
                "CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);"
                "CREATE INDEX IF NOT EXISTS idx_edges_source_type ON edges(source_id, type);"
                "CREATE INDEX IF NOT EXISTS idx_edges_target_type ON edges(target_id, type);"
                "CREATE INDEX IF NOT EXISTS idx_edges_confidence ON edges(confidence);"
            )

        if needs_profiles_migration:
            self._rebuild_table_without_cascade("column_profiles")
            self.conn.executescript("CREATE INDEX IF NOT EXISTS idx_profiles_column ON column_profiles(column_id);")

        self.conn.commit()
        logger.info("Schema migration complete: CASCADE FKs removed")

    def _table_has_cascade_fk(self, table: str, columns: list[str]) -> bool:
        """Check whether a table has ON DELETE CASCADE FKs on given columns."""
        fk_list = self.conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        # PRAGMA foreign_key_list columns: id, seq, table, from, to, on_update, on_delete, match
        for fk in fk_list:
            # fk[3] = "from" column, fk[6] = "on_delete" action
            if fk[3] in columns and fk[6] == "CASCADE":
                return True
        return False

    def _rebuild_table_without_cascade(self, table: str) -> None:
        """Rebuild a table to drop its ON DELETE CASCADE foreign keys.

        Steps:
        1. CREATE TABLE _migrate_{table} with the new schema (no CASCADE FKs)
        2. INSERT INTO _migrate_{table} SELECT * FROM {table}
        3. DROP TABLE {table}
        4. ALTER TABLE _migrate_{table} RENAME TO {table}
        """
        new_schema_sql = {
            "edges": (
                "CREATE TABLE _migrate_edges ("
                "id TEXT PRIMARY KEY,"
                "source_id TEXT NOT NULL,"
                "target_id TEXT NOT NULL,"
                "type TEXT NOT NULL,"
                "confidence REAL DEFAULT 1.0,"
                "properties TEXT,"
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            ),
            "column_profiles": (
                "CREATE TABLE _migrate_column_profiles ("
                "id TEXT PRIMARY KEY,"
                "column_id TEXT NOT NULL,"
                "properties TEXT,"
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            ),
        }

        if table not in new_schema_sql:
            return

        # Disable FK checks during migration so we can drop the old table
        # without cascading (the data is preserved via the temp table)
        self.conn.execute("PRAGMA foreign_keys = OFF")

        self.conn.execute(new_schema_sql[table])
        self.conn.execute(f"INSERT INTO _migrate_{table} SELECT * FROM {table}")
        self.conn.execute(f"DROP TABLE {table}")
        self.conn.execute(f"ALTER TABLE _migrate_{table} RENAME TO {table}")

        # Re-enable FK checks
        self.conn.execute("PRAGMA foreign_keys = ON")

    # --- Internal helpers ---

    def _row_to_node(self, row: tuple) -> Node:
        """Convert a database row to a Node model."""
        id_str, type_str, name, properties_json = row
        node_type = NodeType(type_str)
        properties = json.loads(properties_json) if properties_json else {}

        if node_type == NodeType.COLUMN:
            return ColumnNode(
                id=id_str,
                name=name,
                table_id=properties.get("table_id", ""),
                dtype=properties.get("dtype", ""),
                semantic_type=properties.get("semantic_type", ""),
                comment=properties.get("comment", ""),
                profile_id=properties.get("profile_id", ""),
                properties=properties,
            )
        elif node_type == NodeType.TABLE:
            return TableNode(
                id=id_str,
                name=name,
                source=properties.get("source", ""),
                row_count=properties.get("row_count", 0),
                column_ids=properties.get("column_ids", []),
                properties=properties,
            )
        elif node_type == NodeType.CONCEPT:
            return ConceptNode(
                id=id_str,
                name=name,
                description=properties.get("description", ""),
                unit=properties.get("unit", ""),
                dimension=properties.get("dimension", ""),
                properties=properties,
            )
        elif node_type == NodeType.ENTITY:
            return EntityNode(
                id=id_str,
                name=name,
                description=properties.get("description", ""),
                properties=properties,
            )
        else:
            return Node(id=id_str, type=node_type, name=name, properties=properties)

    def _row_to_edge(self, row: tuple) -> Edge:
        """Convert a database row to an Edge model."""
        id_str, source_id, target_id, type_str, confidence, properties_json = row
        return Edge(
            id=id_str,
            source_id=source_id,
            target_id=target_id,
            type=EdgeType(type_str),
            confidence=confidence,
            properties=json.loads(properties_json) if properties_json else {},
        )

    def _row_to_profile(self, row: tuple) -> ColumnProfile:
        """Convert a database row to a ColumnProfile model."""
        id_str, column_id, properties_json = row
        props = json.loads(properties_json) if properties_json else {}
        return ColumnProfile(id=id_str, column_id=column_id, **props)

    def _row_to_pending_edge(self, row: tuple) -> PendingEdge:
        """Convert a database row to a PendingEdge model."""
        id_str, source_id, target_id, type_str, confidence, properties_json, missing_json = row
        return PendingEdge(
            id=id_str,
            source_id=source_id,
            target_id=target_id,
            type=EdgeType(type_str),
            confidence=confidence,
            properties=json.loads(properties_json) if properties_json else {},
            missing_endpoints=json.loads(missing_json) if missing_json else [],
        )
