"""Tests for node ID parsing with colon-rich sources."""

from datalink.utils.ids import parse_column_id, parse_table_id


class TestParseTableId:
    def test_simple_path(self):
        parsed = parse_table_id("table:/data/orders.csv:orders")
        assert parsed is not None
        assert parsed.source == "/data/orders.csv"
        assert parsed.table_name == "orders"

    def test_sqlite_url_with_hyphenated_table(self):
        url = "sqlite:////home/datalink/datalink/dacomp-zh-006.sqlite"
        parsed = parse_table_id(f"table:{url}:dacomp-zh-006")
        assert parsed is not None
        assert parsed.source == url
        assert parsed.table_name == "dacomp-zh-006"

    def test_postgres_url_with_credentials(self):
        url = "postgresql://admin:s3cret@host:5432/mydb"
        parsed = parse_table_id(f"table:{url}:orders")
        assert parsed is not None
        assert parsed.source == url
        assert parsed.table_name == "orders"

    def test_invalid(self):
        assert parse_table_id("column:x:y:z") is None
        assert parse_table_id("table:nosegments") is None


class TestParseColumnId:
    def test_simple(self):
        parsed = parse_column_id("column:/data/file.csv:users:email")
        assert parsed is not None
        assert parsed.source == "/data/file.csv"
        assert parsed.table_name == "users"
        assert parsed.column_name == "email"

    def test_sqlite_url(self):
        url = "sqlite:////home/datalink/datalink/dacomp-zh-006.sqlite"
        parsed = parse_column_id(f"column:{url}:dacomp-zh-006:amount")
        assert parsed is not None
        assert parsed.source == url
        assert parsed.table_name == "dacomp-zh-006"
        assert parsed.column_name == "amount"

    def test_naive_split_would_fail(self):
        """Document why split(':')[2] is wrong for sqlite URLs."""
        col_id = "column:sqlite:////home/datalink/x.sqlite:dacomp-zh-006:amount"
        naive = col_id.split(":")
        assert naive[2] == "////home/datalink/x.sqlite"  # path fragment — not table name
        parsed = parse_column_id(col_id)
        assert parsed is not None
        assert parsed.table_name == "dacomp-zh-006"
        assert parsed.column_name == "amount"
