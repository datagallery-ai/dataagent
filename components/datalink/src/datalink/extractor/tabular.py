"""Extract structural nodes from tabular datasource info."""

import logging

from datalink.models.datasource import DatasourceInfo
from datalink.models.edge import Edge, EdgeType
from datalink.models.node import ColumnNode, TableNode

logger = logging.getLogger(__name__)


def generate_id(*parts: str) -> str:
    """Generate a deterministic node/edge ID from parts.

    Format: type:source:table:column (for columns) or type:source:table (for tables).
    """
    return ":".join(parts)


class TabularExtractor:
    """Extract TableNode, ColumnNode, and explicit edges from tabular datasource info."""

    def extract(self, datasource_info: DatasourceInfo) -> tuple[list[TableNode], list[ColumnNode], list[Edge]]:
        """Extract all structural nodes and explicit edges.

        Returns:
            Tuple of (table_nodes, column_nodes, edges) where edges include
            both 'contains' and 'foreign_key' edges.
        """
        # Each table_info.source contains the real path/connection_string
        # (set by FileConnector as str(config.path) and DatabaseConnector as
        # config.connection_string). Use that as the stable identifier for
        # node IDs and rebuild reconnection.
        tables: list[TableNode] = []
        columns: list[ColumnNode] = []
        edges: list[Edge] = []

        for table_info in datasource_info.tables:
            source_name = (
                table_info.source
                or datasource_info.config.path
                or datasource_info.config.connection_string
                or "unknown"
            )

            # Create TableNode
            table_id = generate_id("table", source_name, table_info.name)
            table_node = TableNode(
                id=table_id,
                name=table_info.name,
                source=source_name,
                row_count=table_info.row_count or 0,
                properties={
                    "schema_name": table_info.schema_name,
                    "comment": table_info.comment,
                },
            )
            tables.append(table_node)

            # Create ColumnNodes and contains edges
            column_ids = []
            for col_info in table_info.columns:
                col_id = generate_id("column", source_name, table_info.name, col_info.name)
                col_node = ColumnNode(
                    id=col_id,
                    name=col_info.name,
                    table_id=table_id,
                    dtype=col_info.dtype,
                    comment=col_info.comment,
                    properties={
                        "nullable": col_info.nullable,
                        "is_primary_key": col_info.is_primary_key,
                        "default_value": (str(col_info.default_value) if col_info.default_value else ""),
                    },
                )
                columns.append(col_node)
                column_ids.append(col_id)

                # contains edge: Table → Column
                contains_edge = Edge(
                    id=generate_id("edge", "contains", table_id, col_id),
                    source_id=table_id,
                    target_id=col_id,
                    type=EdgeType.CONTAINS,
                )
                edges.append(contains_edge)

            # Update table's column_ids
            table_node.column_ids = column_ids

            # Create foreign_key edges
            for fk in table_info.foreign_keys:
                fk_source_id = generate_id("column", source_name, fk.source_table, fk.source_column)
                fk_target_id = generate_id("column", source_name, fk.target_table, fk.target_column)
                fk_edge = Edge(
                    id=generate_id(
                        "edge",
                        "fk",
                        fk.source_table,
                        fk.source_column,
                        fk.target_table,
                        fk.target_column,
                    ),
                    source_id=fk_source_id,
                    target_id=fk_target_id,
                    type=EdgeType.FOREIGN_KEY,
                    properties={
                        "constraint_name": fk.constraint_name,
                    },
                )
                edges.append(fk_edge)

        logger.info(f"Extracted {len(tables)} tables, {len(columns)} columns, {len(edges)} edges")
        return tables, columns, edges
