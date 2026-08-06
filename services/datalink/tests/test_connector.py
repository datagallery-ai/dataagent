"""Tests for data source connectors."""

import sqlite3
from pathlib import Path

import pytest

from datalink.connector.database import DatabaseConnector
from datalink.connector.file import FileConnector
from datalink.models.datasource import (
    DatasourceConfig,
    DatasourceType,
)


class TestFileConnector:
    """Test the CSV/Parquet file connector."""

    def test_connect_single_csv(self, users_csv_path):
        config = DatasourceConfig(
            type=DatasourceType.CSV,
            name="test_users",
            path=str(users_csv_path),
        )
        connector = FileConnector(config)
        connector.connect()
        assert "users" in connector._dataframes

    def test_connect_directory(self, all_csv_paths):
        # all_csv_paths is under test_data directory
        config = DatasourceConfig(
            type=DatasourceType.CSV,
            name="test_all",
            path=str(all_csv_paths[0].parent),
        )
        connector = FileConnector(config)
        connector.connect()
        assert "users" in connector._dataframes
        assert "orders" in connector._dataframes
        assert "transactions" in connector._dataframes

    def test_get_datasource_info(self, users_csv_path):
        config = DatasourceConfig(
            type=DatasourceType.CSV,
            name="test_users",
            path=str(users_csv_path),
        )
        connector = FileConnector(config)
        info = connector.get_datasource_info()

        assert len(info.tables) == 1
        table = info.tables[0]
        assert table.name == "users"
        assert table.row_count == 10
        assert len(table.columns) == 5  # id, name, email, signup_date, age

        # Check specific columns
        col_names = [c.name for c in table.columns]
        assert "id" in col_names
        assert "email" in col_names

        # Check sample data exists
        assert "users" in info.sample_data
        assert len(info.sample_data["users"]) == 10

    def test_get_datasource_info_all_tables(self, all_csv_paths):
        config = DatasourceConfig(
            type=DatasourceType.CSV,
            name="test_all",
            path=str(all_csv_paths[0].parent),
        )
        connector = FileConnector(config)
        info = connector.get_datasource_info()

        assert len(info.tables) == 3
        table_names = [t.name for t in info.tables]
        assert "users" in table_names
        assert "orders" in table_names
        assert "transactions" in table_names

    def test_get_sample_data(self, users_csv_path):
        config = DatasourceConfig(
            type=DatasourceType.CSV,
            path=str(users_csv_path),
        )
        connector = FileConnector(config)
        connector.connect()
        sample = connector.get_sample_data("users", n=5)
        assert len(sample) == 5
        assert "id" in sample[0]
        assert "name" in sample[0]

    def test_dtype_inference(self, users_csv_path):
        config = DatasourceConfig(
            type=DatasourceType.CSV,
            path=str(users_csv_path),
        )
        connector = FileConnector(config)
        info = connector.get_datasource_info()
        table = info.tables[0]

        # Check dtype classifications
        col_map = {c.name: c for c in table.columns}
        assert col_map["id"].dtype == "integer"
        assert col_map["name"].dtype == "string"
        assert col_map["email"].dtype == "string"
        assert col_map["age"].dtype == "integer"

    def test_disconnect(self, users_csv_path):
        config = DatasourceConfig(
            type=DatasourceType.CSV,
            path=str(users_csv_path),
        )
        connector = FileConnector(config)
        connector.connect()
        assert len(connector._dataframes) > 0
        connector.disconnect()
        assert len(connector._dataframes) == 0

    def test_nonexistent_path(self):
        config = DatasourceConfig(
            type=DatasourceType.CSV,
            path="/nonexistent/path.csv",
        )
        connector = FileConnector(config)
        with pytest.raises(ValueError, match="does not exist"):
            connector.connect()

    def test_id_as_primary_key(self, users_csv_path):
        config = DatasourceConfig(
            type=DatasourceType.CSV,
            path=str(users_csv_path),
        )
        connector = FileConnector(config)
        info = connector.get_datasource_info()
        table = info.tables[0]
        col_map = {c.name: c for c in table.columns}
        # id column is unique and not nullable → should be detected as PK
        assert col_map["id"].is_primary_key


def _make_sqlite_db(path: Path, *, hyphenated: bool = False) -> None:
    """Create a tiny SQLite DB with one table for connector tests."""
    conn = sqlite3.connect(path)
    try:
        if hyphenated:
            conn.execute('CREATE TABLE "dacomp-zh-006" (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)')
            conn.execute("INSERT INTO \"dacomp-zh-006\" (id, name, age) VALUES (1, 'Alice', 30), (2, 'Bob', 25)")
        else:
            conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
            conn.execute("INSERT INTO users (id, name, age) VALUES (1, 'Alice', 30), (2, 'Bob', 25)")
        conn.commit()
    finally:
        conn.close()


class TestDatabaseConnectorSqlite:
    """SQLite-specific DatabaseConnector behavior."""

    def test_extract_schema_preserves_sqlite_extension(self, tmp_path):
        """Dots in SQLite file paths must not be treated as db.schema."""
        db_path = tmp_path / "dacomp-zh-006.sqlite"
        _make_sqlite_db(db_path)
        url = f"sqlite:///{db_path.as_posix()}"

        config = DatasourceConfig(
            type=DatasourceType.DATABASE,
            connection_string=url,
        )
        connector = DatabaseConnector(config)
        clean_url, schema = connector._extract_schema_from_url(url)

        assert schema is None
        assert clean_url == url
        assert clean_url.endswith(".sqlite")

    def test_connect_clears_public_schema(self, tmp_path):
        db_path = tmp_path / "app.db"
        _make_sqlite_db(db_path)
        url = f"sqlite:///{db_path.as_posix()}"

        config = DatasourceConfig(
            type=DatasourceType.DATABASE,
            connection_string=url,
            schema_name="public",
        )
        connector = DatabaseConnector(config)
        connector.connect()
        try:
            assert connector.config.schema_name is None
        finally:
            connector.disconnect()

    def test_get_datasource_info_with_sqlite_extension(self, tmp_path):
        """End-to-end: .sqlite path must introspect tables without public.sqlite_master."""
        db_path = tmp_path / "dacomp-zh-006.sqlite"
        _make_sqlite_db(db_path)
        url = f"sqlite:///{db_path.as_posix()}"

        config = DatasourceConfig(
            type=DatasourceType.DATABASE,
            connection_string=url,
        )
        connector = DatabaseConnector(config)
        info = connector.get_datasource_info()
        connector.disconnect()

        assert len(info.tables) == 1
        assert info.tables[0].name == "users"
        assert info.tables[0].row_count == 2
        col_names = [c.name for c in info.tables[0].columns]
        assert "id" in col_names
        assert "name" in col_names
        assert "users" in info.sample_data
        assert len(info.sample_data["users"]) == 2

    def test_hyphenated_table_name_row_count_and_sample(self, tmp_path):
        """Table names with hyphens must be quoted in COUNT/sample SQL."""
        db_path = tmp_path / "dacomp-zh-006.sqlite"
        _make_sqlite_db(db_path, hyphenated=True)
        url = f"sqlite:///{db_path.as_posix()}"

        config = DatasourceConfig(
            type=DatasourceType.DATABASE,
            connection_string=url,
        )
        connector = DatabaseConnector(config)
        info = connector.get_datasource_info()
        connector.disconnect()

        assert len(info.tables) == 1
        assert info.tables[0].name == "dacomp-zh-006"
        assert info.tables[0].row_count == 2
        assert "dacomp-zh-006" in info.sample_data
        assert len(info.sample_data["dacomp-zh-006"]) == 2

    def test_qualified_table_name_quotes_hyphens(self, tmp_path):
        db_path = tmp_path / "app.db"
        _make_sqlite_db(db_path)
        url = f"sqlite:///{db_path.as_posix()}"
        config = DatasourceConfig(type=DatasourceType.DATABASE, connection_string=url)
        connector = DatabaseConnector(config)
        connector.connect()
        try:
            assert connector._qualified_table_name("dacomp-zh-006") == '"dacomp-zh-006"'
        finally:
            connector.disconnect()
