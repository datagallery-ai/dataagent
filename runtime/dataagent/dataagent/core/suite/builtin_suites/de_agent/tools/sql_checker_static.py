"""Statement parsing and target extraction for the SQL checker."""

from __future__ import annotations

import sqlglot
from sqlglot import exp


def parse_artifact(sql: str, kind: str) -> exp.Expression:
    """Require exactly one CREATE TABLE or INSERT statement in the intended dialect."""
    dialect = "hive" if kind == "ddl" else "spark"
    statements = [
        item for item in sqlglot.parse(sql, read=dialect) if item is not None and not isinstance(item, exp.Semicolon)
    ]
    if len(statements) != 1:
        raise ValueError(f"{kind} must contain exactly one statement")
    statement = next(iter(statements))
    required = exp.Create if kind == "ddl" else exp.Insert
    if not isinstance(statement, required):
        raise ValueError(f"{kind} must be {required.__name__}")
    if kind == "ddl" and statement.args.get("kind", "").upper() != "TABLE":
        raise ValueError("DDL must create a table")
    return statement


def table_name(statement: exp.Expression) -> str:
    """Return the explicit target table for a CREATE or INSERT statement."""
    target = statement.this
    if isinstance(target, exp.Schema):
        target = target.this
    if not isinstance(target, exp.Table):
        raise ValueError("An explicit target table is required")
    return ".".join(part.sql(dialect="spark").lower() for part in target.parts)
