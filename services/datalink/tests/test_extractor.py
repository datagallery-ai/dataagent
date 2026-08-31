"""Tests for the tabular extractor."""

from datalink.connector.file import FileConnector
from datalink.extractor.tabular import TabularExtractor, generate_id
from datalink.models.datasource import DatasourceConfig, DatasourceType
from datalink.models.edge import EdgeType
from datalink.models.node import NodeType


class TestGenerateId:
    """Test the ID generation function."""

    def test_simple_id(self):
        id_str = generate_id("table", "mydb", "orders")
        assert id_str == "table:mydb:orders"

    def test_column_id(self):
        id_str = generate_id("column", "mydb", "orders", "customer_id")
        assert id_str == "column:mydb:orders:customer_id"


class TestTabularExtractor:
    """Test the tabular data extractor."""

    def test_extract_single_table(self, users_csv_path):
        config = DatasourceConfig(
            type=DatasourceType.CSV,
            name="test",
            path=str(users_csv_path),
        )
        connector = FileConnector(config)
        ds_info = connector.get_datasource_info()

        extractor = TabularExtractor()
        tables, columns, edges = extractor.extract(ds_info)

        assert len(tables) == 1
        assert tables[0].name == "users"
        assert tables[0].type == NodeType.TABLE
        assert tables[0].row_count == 10

        # Should have 5 columns (id, name, email, signup_date, age)
        assert len(columns) == 5
        col_names = [c.name for c in columns]
        assert "id" in col_names
        assert "email" in col_names

        # All columns should reference the table
        for col in columns:
            assert col.table_id == tables[0].id
            assert col.type == NodeType.COLUMN

        # Should have 5 contains edges + 0 FK edges
        contains_edges = [e for e in edges if e.type == EdgeType.CONTAINS]
        fk_edges = [e for e in edges if e.type == EdgeType.FOREIGN_KEY]
        assert len(contains_edges) == 5
        assert len(fk_edges) == 0

    def test_extract_multiple_tables(self, all_csv_paths):
        config = DatasourceConfig(
            type=DatasourceType.CSV,
            name="test",
            path=str(all_csv_paths[0].parent),
        )
        connector = FileConnector(config)
        ds_info = connector.get_datasource_info()

        extractor = TabularExtractor()
        tables, columns, edges = extractor.extract(ds_info)

        assert len(tables) == 3
        table_names = [t.name for t in tables]
        assert "users" in table_names
        assert "orders" in table_names
        assert "transactions" in table_names

        # Total columns: 5 (users) + 5 (orders) + 5 (transactions) = 15
        assert len(columns) == 15

        # Total contains edges = 15
        contains_edges = [e for e in edges if e.type == EdgeType.CONTAINS]
        assert len(contains_edges) == 15

    def test_table_column_ids_consistency(self, users_csv_path):
        config = DatasourceConfig(
            type=DatasourceType.CSV,
            name="test",
            path=str(users_csv_path),
        )
        connector = FileConnector(config)
        ds_info = connector.get_datasource_info()

        extractor = TabularExtractor()
        tables, columns, _ = extractor.extract(ds_info)

        # Table's column_ids should match the extracted columns
        table = tables[0]
        table_column_ids = set(table.column_ids)
        extracted_column_ids = {c.id for c in columns}
        assert table_column_ids == extracted_column_ids

    def test_column_properties(self, users_csv_path):
        config = DatasourceConfig(
            type=DatasourceType.CSV,
            name="test",
            path=str(users_csv_path),
        )
        connector = FileConnector(config)
        ds_info = connector.get_datasource_info()

        extractor = TabularExtractor()
        _, columns, _ = extractor.extract(ds_info)

        # Check column properties
        id_col = [c for c in columns if c.name == "id"][0]
        assert id_col.dtype == "integer"
        assert id_col.comment == ""  # CSV has no comments
