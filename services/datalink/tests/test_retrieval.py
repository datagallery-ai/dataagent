"""Tests for the retrieval interfaces."""

import os
import tempfile
from pathlib import Path

import pytest

from datalink.builder.pipeline import BuildPipeline
from datalink.config import DataLinkConfig, LLMConfig
from datalink.graph.retrieval import GraphRetrieval
from datalink.models.datasource import DatasourceConfig, DatasourceType
from datalink.models.edge import EdgeType
from datalink.models.node import NodeType

TEST_DATA_DIR = Path(__file__).parent / "test_data"


@pytest.fixture
def populated_graph():
    """Create a populated graph for retrieval testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    config = DataLinkConfig(
        graph_db_path=db_path,
        llm=LLMConfig(model="gpt-4o", api_key=""),
    )
    pipeline = BuildPipeline(config)

    # Build from test data
    ds_config = DatasourceConfig(
        type=DatasourceType.CSV,
        name="test",
        path=str(TEST_DATA_DIR),
    )
    pipeline.init_build([ds_config])

    retrieval = GraphRetrieval(pipeline.storage)
    yield retrieval, pipeline

    pipeline.close()
    if os.path.exists(db_path):
        os.unlink(db_path)


class TestSearchNodes:
    """Test node search."""

    def test_search_by_name(self, populated_graph):
        retrieval, _ = populated_graph
        results = retrieval.search_nodes("customer_id")
        assert len(results) > 0
        assert any(r["name"] == "customer_id" for r in results)

    def test_search_by_type(self, populated_graph):
        retrieval, _ = populated_graph
        results = retrieval.search_nodes("id", node_type=NodeType.COLUMN)
        # Should find columns with "id" in name
        for r in results:
            assert r["type"] == "column"

    def test_search_with_edge_summary(self, populated_graph):
        retrieval, _ = populated_graph
        results = retrieval.search_nodes("customer_id")
        assert len(results) > 0
        result = results[0]
        assert result["edge_count"] > 0
        assert len(result["edges_summary"]) > 0

    def test_search_empty_query(self, populated_graph):
        retrieval, _ = populated_graph
        results = retrieval.search_nodes("")
        # Empty query should still return something (all names match)
        # Actually substring match on "" matches everything
        # But we limit to 10
        assert len(results) <= 10

    def test_search_nonexistent(self, populated_graph):
        retrieval, _ = populated_graph
        results = retrieval.search_nodes("xyz_nonexistent")
        assert len(results) == 0


class TestGetNode:
    """Test node detail retrieval."""

    def test_get_column_node(self, populated_graph):
        retrieval, _ = populated_graph
        # Find a known column node ID
        columns = retrieval.storage.get_nodes_by_type(NodeType.COLUMN)
        col = [c for c in columns if c.name == "customer_id"][0]

        result = retrieval.get_node(col.id)
        assert result is not None
        assert result["type"] == "column"
        assert result["name"] == "customer_id"
        assert len(result["edges"]) > 0

    def test_get_node_with_profile(self, populated_graph):
        retrieval, _ = populated_graph
        columns = retrieval.storage.get_nodes_by_type(NodeType.COLUMN)
        col = [c for c in columns if c.name == "customer_id"][0]

        result = retrieval.get_node(col.id, include_edges=True)
        assert "profile" in result
        assert result["profile"]["dtype"] == "integer"

    def test_get_node_without_edges(self, populated_graph):
        retrieval, _ = populated_graph
        columns = retrieval.storage.get_nodes_by_type(NodeType.COLUMN)
        col = columns[0]

        result = retrieval.get_node(col.id, include_edges=False)
        assert "edges" not in result

    def test_get_nonexistent_node(self, populated_graph):
        retrieval, _ = populated_graph
        result = retrieval.get_node("nonexistent_id")
        assert result is None


class TestFindPaths:
    """Test path finding between nodes."""

    def test_find_path_between_columns(self, populated_graph):
        retrieval, _ = populated_graph
        # Find path from users.id to orders.customer_id
        columns = retrieval.storage.get_nodes_by_type(NodeType.COLUMN)
        users_id = [c for c in columns if c.name == "id" and "users" in c.id][0]
        orders_customer_id = [c for c in columns if c.name == "customer_id"][0]

        paths = retrieval.find_paths(users_id.id, orders_customer_id.id)
        assert len(paths) > 0

        # Check path structure
        path = paths[0]
        assert "nodes" in path
        assert "edges" in path
        assert "confidence" in path
        assert path["nodes"][0] == users_id.id
        assert path["nodes"][-1] == orders_customer_id.id

    def test_find_path_with_type_filter(self, populated_graph):
        retrieval, _ = populated_graph
        columns = retrieval.storage.get_nodes_by_type(NodeType.COLUMN)
        col_a = columns[0]
        col_b = columns[1]

        # Only traverse via joinable edges
        paths = retrieval.find_paths(col_a.id, col_b.id, edge_types=[EdgeType.JOINABLE, EdgeType.SEMANTIC_SYNONYM])
        # Results depend on whether such paths exist
        # At minimum, it should not error
        assert isinstance(paths, list)

    def test_find_path_max_depth(self, populated_graph):
        retrieval, _ = populated_graph
        columns = retrieval.storage.get_nodes_by_type(NodeType.COLUMN)
        col_a = columns[0]
        col_b = columns[-1]

        # Depth 1 should find only direct connections
        paths_d1 = retrieval.find_paths(col_a.id, col_b.id, max_depth=1)
        # Depth 3 should find more
        paths_d3 = retrieval.find_paths(col_a.id, col_b.id, max_depth=3)
        # Not necessarily more paths, but should be at least as many
        assert len(paths_d3) >= len(paths_d1)


class TestExtractSubgraph:
    """Test subgraph extraction."""

    def test_extract_subgraph_from_column(self, populated_graph):
        retrieval, _ = populated_graph
        columns = retrieval.storage.get_nodes_by_type(NodeType.COLUMN)
        col = [c for c in columns if c.name == "customer_id"][0]

        result = retrieval.extract_subgraph([col.id], max_hops=2)
        assert result["stats"]["node_count"] > 1
        assert result["stats"]["edge_count"] > 0

        # The subgraph should contain the starting column
        assert any(n["id"] == col.id for n in result["nodes"])

    def test_extract_subgraph_limited_hops(self, populated_graph):
        retrieval, _ = populated_graph
        columns = retrieval.storage.get_nodes_by_type(NodeType.COLUMN)
        col = columns[0]

        # 1 hop should give fewer nodes than 2 hops
        result_1 = retrieval.extract_subgraph([col.id], max_hops=1)
        result_2 = retrieval.extract_subgraph([col.id], max_hops=2)
        assert result_2["stats"]["node_count"] >= result_1["stats"]["node_count"]


class TestListDatasets:
    """Test dataset listing."""

    def test_list_datasets(self, populated_graph):
        retrieval, _ = populated_graph
        datasets = retrieval.list_datasets()
        assert len(datasets) == 3  # 3 tables from test data

        # Check structure
        for ds in datasets:
            assert "id" in ds
            assert "name" in ds
            assert "column_count" in ds
            assert ds["column_count"] == 5  # All test tables have 5 columns

    def test_list_datasets_contains_table_names(self, populated_graph):
        retrieval, _ = populated_graph
        datasets = retrieval.list_datasets()
        names = [ds["name"] for ds in datasets]
        assert "users" in names
        assert "orders" in names
        assert "transactions" in names
