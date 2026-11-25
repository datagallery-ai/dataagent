"""Observed schema shared by SQL workflows and database exploration."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SchemaContext:
    """The schema slice returned by exploration or a workflow scenario hook.

    Args:
        schema: Selected tables (``{table: {description, columns}}``).
        joins: Joinable column pairs, as ``(table.column, table.column)``.
        schema_str: Handler-made DDL; unlike the default flow it may embed
            derived-metric formulas.
        extra_rules: The sole rule-injection channel, appended to ``sql_rules``.
    """

    schema: dict[str, Any]
    joins: list[tuple[str, str]] = field(default_factory=list)
    schema_str: str = ""
    extra_rules: str = ""
