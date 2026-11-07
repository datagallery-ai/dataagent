"""Tests for the graph storage layer."""

import os
import tempfile

import pytest

from datalink.graph.storage import GraphStorage
from datalink.models.edge import Edge, EdgeType
from datalink.models.node import ColumnNode, ConceptNode, EntityNode, NodeType, TableNode
from datalink.models.profile import ColumnProfile


@pytest.fixture
def storage():
    """Create a temporary graph storage for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        s = GraphStorage(db_path)
        yield s
        s.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


class TestNodeOperations:
    """Test node CRUD operations."""

    def test_add_and_get_table_node(self, storage):
        table = TableNode(
            id="table:test:users",
            name="users",
            source="test_csv",
            row_count=100,
            column_ids=["col:test:users:id"],
        )
        # Store column_ids and other table-specific fields in properties
        # since the DB schema stores everything in a generic properties JSON
        table.properties["source"] = table.source
        table.properties["row_count"] = table.row_count
        table.properties["column_ids"] = table.column_ids

        storage.add_node(table)
        retrieved = storage.get_node("table:test:users")
        assert retrieved is not None
        assert retrieved.name == "users"
        assert retrieved.type == NodeType.TABLE

    def test_add_and_get_column_node(self, storage):
        col = ColumnNode(
            id="column:test:users:id",
            name="id",
            table_id="table:test:users",
            dtype="integer",
            semantic_type="identifier",
        )
        col.properties["table_id"] = col.table_id
        col.properties["dtype"] = col.dtype
        col.properties["semantic_type"] = col.semantic_type

        storage.add_node(col)
        retrieved = storage.get_node("column:test:users:id")
        assert retrieved is not None
        assert retrieved.name == "id"
        assert retrieved.type == NodeType.COLUMN

    def test_add_concept_node(self, storage):
        concept = ConceptNode(
            id="concept:revenue",
            name="revenue",
            description="Total monetary income",
            unit="USD",
        )
        concept.properties["description"] = concept.description
        concept.properties["unit"] = concept.unit

        storage.add_node(concept)
        retrieved = storage.get_node("concept:revenue")
        assert retrieved is not None
        assert retrieved.name == "revenue"
        assert retrieved.type == NodeType.CONCEPT

    def test_add_entity_node(self, storage):
        entity = EntityNode(
            id="entity:customer",
            name="customer",
            description="A person who purchases products",
        )
        entity.properties["description"] = entity.description

        storage.add_node(entity)
        retrieved = storage.get_node("entity:customer")
        assert retrieved is not None
        assert retrieved.name == "customer"
        assert retrieved.type == NodeType.ENTITY

    def test_remove_node_cascades_edges(self, storage):
        # Add nodes and an edge
        storage.add_node(ColumnNode(id="col:a", name="a", table_id="t1", dtype="int"))
        storage.add_node(ColumnNode(id="col:b", name="b", table_id="t2", dtype="int"))
        storage.add_edge(
            Edge(
                id="edge:1",
                source_id="col:a",
                target_id="col:b",
                type=EdgeType.JOINABLE,
                confidence=0.8,
            )
        )

        # Remove col:a — should cascade remove the edge
        storage.remove_node("col:a")

        # Edge should be gone
        edge = storage.get_edge("edge:1")
        assert edge is None
        # col:b should still exist
        node_b = storage.get_node("col:b")
        assert node_b is not None

    def test_get_nodes_by_type(self, storage):
        storage.add_node(TableNode(id="table:t1", name="t1", source="test"))
        storage.add_node(TableNode(id="table:t2", name="t2", source="test"))
        storage.add_node(ColumnNode(id="col:c1", name="c1", table_id="t1", dtype="int"))

        tables = storage.get_nodes_by_type(NodeType.TABLE)
        assert len(tables) == 2

        columns = storage.get_nodes_by_type(NodeType.COLUMN)
        assert len(columns) == 1

    def test_search_nodes_by_name(self, storage):
        storage.add_node(ColumnNode(id="col:users:id", name="id", table_id="t", dtype="int"))
        storage.add_node(ColumnNode(id="col:users:customer_id", name="customer_id", table_id="t", dtype="int"))
        storage.add_node(ColumnNode(id="col:orders:amount", name="amount", table_id="t", dtype="float"))

        # Search for "id" in column names
        results = storage.search_nodes_by_name("id", NodeType.COLUMN)
        assert len(results) == 2  # "id" and "customer_id"

        # Search for "amount" in all types
        results = storage.search_nodes_by_name("amount")
        assert len(results) == 1

    def test_count_nodes(self, storage):
        storage.add_node(TableNode(id="t:1", name="t1", source="s"))
        storage.add_node(ColumnNode(id="c:1", name="c1", table_id="t1", dtype="int"))
        storage.add_node(ColumnNode(id="c:2", name="c2", table_id="t1", dtype="str"))

        assert storage.count_nodes() == 3
        assert storage.count_nodes(NodeType.TABLE) == 1
        assert storage.count_nodes(NodeType.COLUMN) == 2


class TestEdgeOperations:
    """Test edge CRUD operations."""

    def test_add_and_get_edge(self, storage):
        storage.add_node(ColumnNode(id="col:a", name="a", table_id="t1", dtype="int"))
        storage.add_node(ColumnNode(id="col:b", name="b", table_id="t2", dtype="int"))

        edge = Edge(
            id="edge:join1",
            source_id="col:a",
            target_id="col:b",
            type=EdgeType.JOINABLE,
            confidence=0.85,
        )
        storage.add_edge(edge)

        retrieved = storage.get_edge("edge:join1")
        assert retrieved is not None
        assert retrieved.type == EdgeType.JOINABLE
        assert retrieved.confidence == 0.85

    def test_add_edges_batch(self, storage):
        storage.add_node(TableNode(id="table:t1", name="t1", source="s"))
        storage.add_node(ColumnNode(id="col:c1", name="c1", table_id="t1", dtype="int"))
        storage.add_node(ColumnNode(id="col:c2", name="c2", table_id="t1", dtype="str"))

        edges = [
            Edge(id="e:1", source_id="table:t1", target_id="col:c1", type=EdgeType.CONTAINS),
            Edge(id="e:2", source_id="table:t1", target_id="col:c2", type=EdgeType.CONTAINS),
        ]
        storage.add_edges_batch(edges)

        assert storage.count_edges(EdgeType.CONTAINS) == 2

    def test_get_edges_for_node(self, storage):
        storage.add_node(TableNode(id="table:t1", name="t1", source="s"))
        storage.add_node(ColumnNode(id="col:c1", name="c1", table_id="t1", dtype="int"))
        storage.add_node(ColumnNode(id="col:c2", name="c2", table_id="t2", dtype="int"))

        storage.add_edge(Edge(id="e:1", source_id="table:t1", target_id="col:c1", type=EdgeType.CONTAINS))
        storage.add_edge(Edge(id="e:2", source_id="col:c1", target_id="col:c2", type=EdgeType.JOINABLE))

        edges = storage.get_edges_for_node("table:t1")
        assert len(edges) == 1  # only the contains edge

        edges = storage.get_edges_for_node("col:c1")
        assert len(edges) == 2  # both contains (incoming) and joinable (outgoing)

    def test_remove_edge(self, storage):
        storage.add_node(ColumnNode(id="col:a", name="a", table_id="t1", dtype="int"))
        storage.add_node(ColumnNode(id="col:b", name="b", table_id="t2", dtype="int"))
        storage.add_edge(Edge(id="e:1", source_id="col:a", target_id="col:b", type=EdgeType.JOINABLE))

        storage.remove_edge("e:1")
        assert storage.get_edge("e:1") is None

    def test_count_edges(self, storage):
        storage.add_node(TableNode(id="t1", name="t1", source="s"))
        storage.add_node(ColumnNode(id="c1", name="c1", table_id="t1", dtype="int"))
        storage.add_node(ColumnNode(id="c2", name="c2", table_id="t2", dtype="int"))

        storage.add_edge(Edge(id="e:1", source_id="t1", target_id="c1", type=EdgeType.CONTAINS))
        storage.add_edge(Edge(id="e:2", source_id="c1", target_id="c2", type=EdgeType.JOINABLE))

        assert storage.count_edges() == 2
        assert storage.count_edges(EdgeType.CONTAINS) == 1


class TestProfileOperations:
    """Test column profile CRUD."""

    def test_add_and_get_profile(self, storage):
        storage.add_node(ColumnNode(id="col:t1:amount", name="amount", table_id="t1", dtype="float"))

        profile = ColumnProfile(
            id="profile:col:t1:amount",
            column_id="col:t1:amount",
            dtype="float",
            semantic_type="monetary_value",
            null_rate=0.05,
            min_value=10.0,
            max_value=500.0,
        )
        storage.add_profile(profile)

        retrieved = storage.get_profile("profile:col:t1:amount")
        assert retrieved is not None
        assert retrieved.dtype == "float"
        assert retrieved.semantic_type == "monetary_value"

    def test_get_profile_for_column(self, storage):
        storage.add_node(ColumnNode(id="col:t1:amount", name="amount", table_id="t1", dtype="float"))

        profile = ColumnProfile(
            id="profile:col:t1:amount",
            column_id="col:t1:amount",
            dtype="float",
        )
        storage.add_profile(profile)

        retrieved = storage.get_profile_for_column("col:t1:amount")
        assert retrieved is not None
        assert retrieved.column_id == "col:t1:amount"

    def test_add_profiles_batch(self, storage):
        storage.add_node(ColumnNode(id="c1", name="c1", table_id="t1", dtype="int"))
        storage.add_node(ColumnNode(id="c2", name="c2", table_id="t2", dtype="str"))

        profiles = [
            ColumnProfile(id="p1", column_id="c1", dtype="int"),
            ColumnProfile(id="p2", column_id="c2", dtype="str"),
        ]
        storage.add_profiles_batch(profiles)
        assert storage.get_profile("p1") is not None
        assert storage.get_profile("p2") is not None


class TestRemoveTable:
    """Test the remove_table operation with cascading deletes."""

    def test_remove_table_removes_columns_and_edges(self, storage):
        # Add a table with two columns
        storage.add_node(TableNode(id="table:test:orders", name="orders", source="test"))
        storage.add_node(
            ColumnNode(id="col:test:orders:order_id", name="order_id", table_id="table:test:orders", dtype="int")
        )
        storage.add_node(
            ColumnNode(id="col:test:orders:amount", name="amount", table_id="table:test:orders", dtype="float")
        )

        # Add contains edges
        storage.add_edge(
            Edge(id="e:1", source_id="table:test:orders", target_id="col:test:orders:order_id", type=EdgeType.CONTAINS)
        )
        storage.add_edge(
            Edge(id="e:2", source_id="table:test:orders", target_id="col:test:orders:amount", type=EdgeType.CONTAINS)
        )

        # Add a profile
        storage.add_profile(ColumnProfile(id="p:1", column_id="col:test:orders:order_id", dtype="int"))

        # Remove the table
        removed_cols = storage.remove_table("table:test:orders")

        assert len(removed_cols) == 2
        assert storage.get_node("table:test:orders") is None
        assert storage.get_node("col:test:orders:order_id") is None
        assert storage.get_node("col:test:orders:amount") is None
        assert storage.get_edge("e:1") is None
        assert storage.get_edge("e:2") is None
        assert storage.get_profile("p:1") is None


class TestCleanupOrphanedSemanticNodes:
    """Test cleanup of orphaned Concept/Entity nodes."""

    def test_cleanup_removes_orphaned_concepts(self, storage):
        # Add a concept with a represents edge
        storage.add_node(ConceptNode(id="concept:revenue", name="revenue", description="Income"))
        storage.add_node(ColumnNode(id="col:orders:amount", name="amount", table_id="t1", dtype="float"))
        storage.add_edge(
            Edge(
                id="e:repr:1",
                source_id="col:orders:amount",
                target_id="concept:revenue",
                type=EdgeType.REPRESENTS,
                confidence=0.9,
            )
        )

        # Add an orphaned concept (no represents edges)
        storage.add_node(ConceptNode(id="concept:orphan", name="orphan", description="No connections"))

        # Cleanup should remove the orphan
        removed = storage.cleanup_orphaned_semantic_nodes()
        assert removed == 1
        assert storage.get_node("concept:orphan") is None
        assert storage.get_node("concept:revenue") is not None  # Still connected

    def test_entity_with_has_concept_edges_is_orphan_without_structural_anchor(self, storage):
        # Entity connected only to other semantic nodes (has_concept) IS orphaned
        # — has_concept edges are semantic-layer-internal and don't count as
        # anchors.  Only represents edges from structural nodes anchor a
        # semantic node.
        storage.add_node(EntityNode(id="entity:customer", name="customer", description="A person"))
        storage.add_node(ConceptNode(id="concept:customer_id", name="customer_id", description="ID"))
        storage.add_edge(
            Edge(
                id="e:hc:1",
                source_id="entity:customer",
                target_id="concept:customer_id",
                type=EdgeType.HAS_CONCEPT,
            )
        )

        removed = storage.cleanup_orphaned_semantic_nodes()
        assert removed == 2  # Both entity and concept are orphaned — no structural anchor
        # Their has_concept edge should also be cleaned up
        assert storage.count_edges(EdgeType.HAS_CONCEPT) == 0

    def test_entity_with_represents_from_structural_is_not_orphan(self, storage):
        # Entity IS anchored when a structural node (Column) has a represents
        # edge targeting its associated Concept.
        storage.add_node(ColumnNode(id="col:1", name="customer_id", table_id="t1", dtype="int"))
        storage.add_node(EntityNode(id="entity:customer", name="customer", description="A person"))
        storage.add_node(ConceptNode(id="concept:customer_id", name="customer_id", description="ID"))
        storage.add_edge(
            Edge(
                id="e:rep:1",
                source_id="col:1",
                target_id="concept:customer_id",
                type=EdgeType.REPRESENTS,
            )
        )
        storage.add_edge(
            Edge(
                id="e:hc:1",
                source_id="entity:customer",
                target_id="concept:customer_id",
                type=EdgeType.HAS_CONCEPT,
            )
        )

        removed = storage.cleanup_orphaned_semantic_nodes()
        assert removed == 0  # Concept is anchored by Column → represents edge


class TestGraphStats:
    """Test graph statistics."""

    def test_get_graph_stats(self, storage):
        storage.add_node(TableNode(id="t1", name="t1", source="s"))
        storage.add_node(ColumnNode(id="c1", name="c1", table_id="t1", dtype="int"))
        storage.add_node(ConceptNode(id="concept:1", name="test_concept", description="test"))

        storage.add_edge(Edge(id="e:1", source_id="t1", target_id="c1", type=EdgeType.CONTAINS))
        storage.add_edge(
            Edge(id="e:2", source_id="c1", target_id="concept:1", type=EdgeType.REPRESENTS, confidence=0.9)
        )

        stats = storage.get_graph_stats()
        assert stats["total_nodes"] == 3
        assert stats["node_type_counts"]["table"] == 1
        assert stats["node_type_counts"]["column"] == 1
        assert stats["node_type_counts"]["concept"] == 1
        assert stats["total_edges"] == 2

    def test_clear_all(self, storage):
        storage.add_node(TableNode(id="t1", name="t1", source="s"))
        storage.add_node(ColumnNode(id="c1", name="c1", table_id="t1", dtype="int"))
        storage.clear_all()
        assert storage.count_nodes() == 0


class TestMetadata:
    """Test metadata operations."""

    def test_set_and_get_metadata(self, storage):
        storage.set_metadata("build_time", "2024-01-01T00:00:00")
        assert storage.get_metadata("build_time") == "2024-01-01T00:00:00"

    def test_get_nonexistent_metadata(self, storage):
        assert storage.get_metadata("nonexistent") is None
