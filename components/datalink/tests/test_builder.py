"""Tests for the build pipeline."""

import os
import tempfile

import pytest

from datalink.builder.pipeline import BuildPipeline
from datalink.config import DataLinkConfig, LLMConfig
from datalink.models.datasource import DatasourceConfig, DatasourceType
from datalink.models.node import NodeType


@pytest.fixture
def pipeline():
    """Create a build pipeline with a temporary database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    config = DataLinkConfig(
        graph_db_path=db_path,
        llm=LLMConfig(model="gpt-4o", api_key=""),  # No API key for tests
    )
    pipeline = BuildPipeline(config)
    yield pipeline
    pipeline.close()
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def csv_datasource_config(all_csv_paths):
    """Datasource config pointing to test CSV files."""
    return DatasourceConfig(
        type=DatasourceType.CSV,
        path=str(all_csv_paths[0].parent),
    )


class TestInitBuild:
    """Test initial build from data source configs."""

    def test_build_from_csv_files(self, pipeline, csv_datasource_config):
        result = pipeline.init_build([csv_datasource_config])

        assert result["status"] == "success"
        stats = result["stats"]
        assert stats["total_nodes"] > 0
        assert stats["total_edges"] > 0

        # Should have tables and columns
        assert stats["node_type_counts"]["table"] == 3
        assert stats["node_type_counts"]["column"] == 15

        # Should have contains edges (15 columns)
        assert stats["edge_type_counts"]["contains"] == 15

        # Should have inferred edges
        assert stats["edge_type_counts"]["joinable"] > 0
        assert stats["edge_type_counts"]["semantic_synonym"] > 0

    def test_build_clears_previous_data(self, pipeline, csv_datasource_config):
        # Build twice — second build should replace all data
        pipeline.init_build([csv_datasource_config])
        stats_after_first = pipeline.storage.get_graph_stats()

        pipeline.init_build([csv_datasource_config])
        stats_after_second = pipeline.storage.get_graph_stats()

        # Same number of nodes (full rebuild, not accumulation)
        assert stats_after_first["total_nodes"] == stats_after_second["total_nodes"]

    def test_build_without_llm_skips_semantic_mapping(self, pipeline, csv_datasource_config):
        # No API key configured, so LLM mapping should be skipped
        result = pipeline.init_build([csv_datasource_config])

        # CSV files have no comments, so no metadata mapping either
        # → Should have 0 concepts and 0 entities
        stats = result["stats"]
        assert stats["node_type_counts"]["concept"] == 0
        assert stats["node_type_counts"]["entity"] == 0


class TestRebuild:
    """Test rebuild from existing table metadata."""

    def test_rebuild_from_existing_graph(self, pipeline, csv_datasource_config):
        # First, init build
        result = pipeline.init_build([csv_datasource_config])
        assert result["status"] == "success"
        initial_stats = pipeline.storage.get_graph_stats()

        # Now rebuild
        result = pipeline.rebuild()
        assert result["status"] == "success"
        rebuild_stats = pipeline.storage.get_graph_stats()

        # Should produce the same number of nodes
        assert initial_stats["total_nodes"] == rebuild_stats["total_nodes"]
        assert initial_stats["node_type_counts"]["table"] == rebuild_stats["node_type_counts"]["table"]
        assert initial_stats["node_type_counts"]["column"] == rebuild_stats["node_type_counts"]["column"]

    def test_rebuild_empty_graph(self, pipeline):
        # Rebuild on an empty graph should return error
        result = pipeline.rebuild()
        assert result["status"] == "error"
        assert "No tables found" in result["error"]


class TestAddDatasource:
    """Test incrementally adding tables via add_datasource."""

    def test_add_datasource_all_tables(self, pipeline, all_csv_paths):
        # First build with just users.csv
        users_config = DatasourceConfig(
            type=DatasourceType.CSV,
            path=str(all_csv_paths[0]),  # just users.csv
        )
        pipeline.init_build([users_config])
        initial_stats = pipeline.storage.get_graph_stats()
        assert initial_stats["node_type_counts"]["table"] == 1

        # Now add orders.csv and transactions.csv via add_datasource (all tables)
        orders_config = DatasourceConfig(
            type=DatasourceType.CSV,
            path=str(all_csv_paths[0].parent),  # directory with all 3 CSVs
        )
        result = pipeline.add_datasource(orders_config)
        assert result["status"] == "success"
        assert "orders" in result["added_tables"]
        assert "transactions" in result["added_tables"]

    def test_add_datasource_specific_tables(self, pipeline, all_csv_paths):
        # First build with users.csv
        users_config = DatasourceConfig(
            type=DatasourceType.CSV,
            path=str(all_csv_paths[0]),
        )
        pipeline.init_build([users_config])
        initial_stats = pipeline.storage.get_graph_stats()
        assert initial_stats["node_type_counts"]["table"] == 1

        # Now add only orders via add_datasource with table_names
        orders_config = DatasourceConfig(
            type=DatasourceType.CSV,
            path=str(all_csv_paths[0].parent),
        )
        result = pipeline.add_datasource(orders_config, ["orders"])
        assert result["status"] == "success"
        assert result["added_tables"] == ["orders"]

        updated_stats = pipeline.storage.get_graph_stats()
        assert updated_stats["node_type_counts"]["table"] == 2

    def test_add_datasource_skips_existing_tables(self, pipeline, csv_datasource_config):
        """Adding the same datasource twice should skip existing tables."""
        # First, add all 3 tables
        pipeline.init_build([csv_datasource_config])
        initial_stats = pipeline.storage.get_graph_stats()
        assert initial_stats["node_type_counts"]["table"] == 3

        # Try to add the same datasource again
        result = pipeline.add_datasource(csv_datasource_config)

        # All tables should be skipped
        assert result["status"] == "skipped"
        assert result["added_tables"] == []
        assert len(result["skipped_tables"]) == 3

        # Graph should remain unchanged
        updated_stats = pipeline.storage.get_graph_stats()
        assert updated_stats["total_nodes"] == initial_stats["total_nodes"]

    def test_add_datasource_mixed_new_and_existing(self, pipeline, all_csv_paths):
        """Adding a datasource with some existing tables should skip those and add new ones."""
        # First, add all 3 CSVs from the directory
        directory_config = DatasourceConfig(
            type=DatasourceType.CSV,
            path=str(all_csv_paths[0].parent),
        )
        pipeline.init_build([directory_config])

        # Now try to add 2 of the same tables individually — they should be skipped
        result = pipeline.add_datasource(directory_config, ["users", "orders"])

        assert result["status"] == "skipped"
        assert "users" in result["skipped_tables"]
        assert "orders" in result["skipped_tables"]
        assert result["added_tables"] == []

        # Graph should remain unchanged
        updated_stats = pipeline.storage.get_graph_stats()
        assert updated_stats["node_type_counts"]["table"] == 3


class TestAddTable:
    """Test incrementally adding a single table."""

    def test_add_table_to_existing_graph(self, pipeline, all_csv_paths):
        # First build with just users.csv
        users_config = DatasourceConfig(
            type=DatasourceType.CSV,
            path=str(all_csv_paths[0]),  # just users.csv
        )
        pipeline.init_build([users_config])
        initial_stats = pipeline.storage.get_graph_stats()
        assert initial_stats["node_type_counts"]["table"] == 1
        assert initial_stats["node_type_counts"]["column"] == 5

        # Now add orders.csv as a separate table
        orders_config = DatasourceConfig(
            type=DatasourceType.CSV,
            path=str(all_csv_paths[1]),  # orders.csv
        )
        result = pipeline.add_table(orders_config, "orders")

        assert result["status"] == "success"
        updated_stats = pipeline.storage.get_graph_stats()
        assert updated_stats["node_type_counts"]["table"] == 2
        assert updated_stats["node_type_counts"]["column"] == 10  # 5 + 5

        # Should now have cross-table inferred edges
        assert updated_stats["edge_type_counts"]["joinable"] > 0


class TestRemoveTable:
    """Test removing a table from the graph."""

    def test_remove_table(self, pipeline, csv_datasource_config):
        # Build full graph first
        pipeline.init_build([csv_datasource_config])
        initial_stats = pipeline.storage.get_graph_stats()
        assert initial_stats["node_type_counts"]["table"] == 3

        # Remove the transactions table
        # Find its ID
        tables = pipeline.storage.get_nodes_by_type(NodeType.TABLE)
        transactions_table = [t for t in tables if t.name == "transactions"][0]

        result = pipeline.remove_table(transactions_table.id)
        assert result["status"] == "success"
        assert result["removed_columns"] == 5  # transactions has 5 columns

        updated_stats = pipeline.storage.get_graph_stats()
        assert updated_stats["node_type_counts"]["table"] == 2
        assert updated_stats["node_type_counts"]["column"] == 10  # 15 - 5
