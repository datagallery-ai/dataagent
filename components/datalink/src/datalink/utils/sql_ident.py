"""Dialect-aware SQL identifier quoting for raw SQL strings."""

from __future__ import annotations

import re

# Map connection-string / source prefixes to SQLAlchemy-ish dialect names.
_DIALECT_PREFIXES: tuple[tuple[str, str], ...] = (
    ("postgresql", "postgresql"),
    ("postgres", "postgresql"),
    ("mysql", "mysql"),
    ("mariadb", "mariadb"),
    ("sqlite", "sqlite"),
    ("mssql", "mssql"),
    ("oracle", "oracle"),
    ("clickhouse", "clickhouse"),
)


def dialect_from_source(source: str) -> str | None:
    """Infer SQL dialect from a connection string or node ``source`` field."""
    if not source:
        return None
    lower = source.lower()
    for prefix, dialect in _DIALECT_PREFIXES:
        if lower.startswith(prefix):
            return dialect
    return None


def quote_identifier(name: str, dialect: str | None = None) -> str:
    """Quote a single SQL identifier (table, column, or schema name).

    Always quotes so names with hyphens, spaces, or reserved words are safe.
    """
    if dialect in ("mysql", "mariadb"):
        return "`" + name.replace("`", "``") + "`"
    if dialect == "mssql":
        return "[" + name.replace("]", "]]") + "]"
    # PostgreSQL, SQLite, Oracle, ClickHouse, unknown → ANSI double quotes
    return '"' + name.replace('"', '""') + '"'


def quote_qualified(*parts: str | None, dialect: str | None = None) -> str:
    """Join and quote non-empty identifier parts with dots.

    Example::

        quote_qualified("public", "orders", dialect="postgresql")
        → '"public"."orders"'
    """
    return ".".join(quote_identifier(p, dialect) for p in parts if p)


_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def needs_quoting(name: str) -> bool:
    """Return True if ``name`` is not a plain unquoted SQL identifier."""
    return not bool(_SAFE_IDENT.match(name))
