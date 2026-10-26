"""Render deagent metadata as DDL context without simplifying types or Join evidence."""

import json
from typing import Any


def schema_to_ddl(schema_ir: dict[str, Any], join_relations: list[dict[str, Any]] | None = None) -> str:
    """Render source tables and complete Join metadata as Hive/Spark SQL context.

    Types retain their original metadata spelling. Comments retain complete Join
    semantics and evidence without inferring physical foreign-key constraints.
    """
    blocks = []
    for table_name, table_info in schema_ir.items():
        lines = []
        description = table_info.get("description")
        if description:
            lines.extend(_comment_lines(str(description)))
        qualified_name = _quote_table(table_name)
        columns = table_info.get("columns", {})
        if not columns:
            lines.extend(_comment_lines(f"Table {qualified_name}: column metadata unavailable."))
            lines.append("-- Retrieve column metadata before referencing this table's columns; do not invent columns.")
            blocks.append("\n".join(lines))
            continue
        lines.append(f"CREATE TABLE {qualified_name} (")
        for index, (column_name, column_info) in enumerate(columns.items()):
            column_description = column_info.get("description")
            if column_description:
                lines.extend(_comment_lines(str(column_description), indent="    "))
            examples = column_info.get("example_values")
            if examples is not None:
                rendered_examples = json.dumps(examples, ensure_ascii=False)
                lines.extend(_comment_lines(f"example_values: {rendered_examples}", indent="    "))
            value_type = column_info.get("value_type")
            if not value_type or not str(value_type).strip():
                lines.append("    -- Original field type unavailable; STRING is a Hive/Spark syntax placeholder only.")
                lines.append(
                    "    -- Retrieve the actual type before choosing casts or calculations based on this field."
                )
                value_type = "STRING"
            separator = "," if index < len(columns) - 1 else ""
            lines.append(f"    {_quote_identifier(column_name)} {value_type}{separator}")
        lines.append(");")
        blocks.append("\n".join(lines))

    if join_relations:
        lines = ["-- Retrieved Join relationships (metadata evidence; not physical foreign-key constraints)"]
        for index, relation in enumerate(join_relations, start=1):
            lines.append(f"-- Join {index}:")
            for key, value in relation.items():
                rendered_value = json.dumps(value, ensure_ascii=False)
                lines.extend(_comment_lines(f"{key}: {rendered_value}"))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _quote_identifier(name: str) -> str:
    """Quote a single identifier without treating a dot inside a column as qualification."""
    return "`" + name.replace("`", "``") + "`"


def _quote_table(name: str) -> str:
    """Quote each component of a qualified table name."""
    return ".".join(_quote_identifier(part) for part in name.split("."))


def _comment_lines(text: str, indent: str = "") -> list[str]:
    """Keep every description line inside a SQL comment."""
    return [f"{indent}-- {line}" for line in text.splitlines()]
