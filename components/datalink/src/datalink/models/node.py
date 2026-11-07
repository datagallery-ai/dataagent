"""Node models for DataLink dual-layer architecture."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class NodeType(str, Enum):
    """Types of nodes in the graph."""

    COLUMN = "column"
    TABLE = "table"
    CONCEPT = "concept"
    ENTITY = "entity"


class Node(BaseModel):
    """Base node model. All nodes share these fields."""

    id: str = Field(description="Unique identifier for the node")
    type: NodeType = Field(description="Type of the node")
    name: str = Field(description="Human-readable name")
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional properties as a flexible JSON dict",
    )

    def to_storage_properties(self) -> dict[str, Any]:
        """Merge typed fields into properties dict for DB storage.

        The DB stores only (id, type, name, properties). Typed fields like
        description, dtype, etc. must be copied into properties so they
        survive serialization/deserialization. This method does that merge
        without mutating the model's own properties field.
        """
        merged = dict(self.properties)
        for field_name in self.__class__.model_fields:
            if field_name in ("id", "type", "name", "properties"):
                continue
            value = getattr(self, field_name)
            merged[field_name] = value
        return merged


class ColumnNode(Node):
    """A column in a table — the primary structural node for tabular data."""

    type: NodeType = NodeType.COLUMN
    table_id: str = Field(description="ID of the parent TableNode")
    dtype: str = Field(default="", description="Data type (e.g., integer, varchar, float)")
    semantic_type: str = Field(
        default="", description="Pre-classified semantic type (e.g., email_address, monetary_value)"
    )
    profile_id: str = Field(default="", description="ID of the associated ColumnProfile")
    comment: str = Field(default="", description="Comment/description from metadata (e.g., SQL column comment)")


class TableNode(Node):
    """A table/dataset — container of ColumnNodes."""

    type: NodeType = NodeType.TABLE
    source: str = Field(default="", description="Data source identifier (e.g., postgres://mydb, csv:./data/)")
    source_type: str = Field(
        default="",
        description="Datasource type: 'csv', 'parquet', or 'database'",
    )
    row_count: int = Field(default=0, description="Number of rows in the table")
    column_ids: list[str] = Field(default_factory=list, description="IDs of all ColumnNodes in this table")


class ConceptNode(Node):
    """A measurable concept/attribute in the semantic layer.

    Examples: revenue, market_share, conversion_rate, age.
    Concepts have units/dimensions and can be quantified.
    """

    type: NodeType = NodeType.CONCEPT
    description: str = Field(default="", description="Description of what this concept means")
    unit: str = Field(default="", description="Unit of measurement (e.g., USD, %, count)")
    dimension: str = Field(default="", description="Dimension/category (e.g., monetary, temporal, demographic)")


class EntityNode(Node):
    """An identifiable thing/collection in the semantic layer.

    Examples: Q3_performance, a_customer, east_china_region.
    Entities have identity/name and group multiple Concepts.
    """

    type: NodeType = NodeType.ENTITY
    description: str = Field(default="", description="Description of what this entity represents")
