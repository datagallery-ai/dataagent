"""Tests for SQL identifier quoting helpers."""

from datalink.utils.sql_ident import (
    dialect_from_source,
    needs_quoting,
    quote_identifier,
    quote_qualified,
)


class TestDialectFromSource:
    def test_sqlite(self):
        assert dialect_from_source("sqlite:////tmp/x.db") == "sqlite"

    def test_postgres(self):
        assert dialect_from_source("postgresql://u:p@h/db") == "postgresql"

    def test_mysql(self):
        assert dialect_from_source("mysql+pymysql://u:p@h/db") == "mysql"

    def test_unknown(self):
        assert dialect_from_source("/path/to/file.csv") is None


class TestQuoteIdentifier:
    def test_sqlite_hyphen(self):
        assert quote_identifier("dacomp-zh-006", "sqlite") == '"dacomp-zh-006"'

    def test_mysql_hyphen(self):
        assert quote_identifier("dacomp-zh-006", "mysql") == "`dacomp-zh-006`"

    def test_mssql(self):
        assert quote_identifier("order-items", "mssql") == "[order-items]"

    def test_embedded_quote_escaped(self):
        assert quote_identifier('a"b', "sqlite") == '"a""b"'

    def test_qualified(self):
        assert quote_qualified("myschema", "dacomp-zh-006", dialect="postgresql") == ('"myschema"."dacomp-zh-006"')

    def test_needs_quoting(self):
        assert needs_quoting("dacomp-zh-006")
        assert not needs_quoting("orders")
