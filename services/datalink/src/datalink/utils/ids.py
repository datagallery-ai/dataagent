"""Parse DataLink node IDs whose source segment may contain colons.

ID formats:
  table:<source>:<table_name>
  column:<source>:<table_name>:<column_name>

``source`` is often a SQLAlchemy URL (e.g. ``sqlite:////path/file.db`` or
``postgresql://user:pass@host/db``), so naive ``str.split(":")`` is incorrect.
Parse from the right: last segment(s) are table/column names.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedTableId:
    source: str
    table_name: str


@dataclass(frozen=True)
class ParsedColumnId:
    source: str
    table_name: str
    column_name: str


def parse_table_id(table_id: str) -> ParsedTableId | None:
    """Parse ``table:<source>:<table_name>``."""
    if not table_id.startswith("table:"):
        return None
    rest = table_id[len("table:") :]
    last = rest.rfind(":")
    if last <= 0:
        return None
    source = rest[:last]
    table_name = rest[last + 1 :]
    if not source or not table_name:
        return None
    return ParsedTableId(source=source, table_name=table_name)


def parse_column_id(column_id: str) -> ParsedColumnId | None:
    """Parse ``column:<source>:<table_name>:<column_name>``."""
    if not column_id.startswith("column:"):
        return None
    rest = column_id[len("column:") :]
    last = rest.rfind(":")
    if last <= 0:
        return None
    second_last = rest.rfind(":", 0, last)
    if second_last <= 0:
        return None
    source = rest[:second_last]
    table_name = rest[second_last + 1 : last]
    column_name = rest[last + 1 :]
    if not source or not table_name or not column_name:
        return None
    return ParsedColumnId(source=source, table_name=table_name, column_name=column_name)
