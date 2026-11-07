"""Edge models for DataLink relationships."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EdgeType(str, Enum):
    """Types of edges in the graph.

    A: Structural explicit (from schema/metadata)
    B: Semantic implicit (inferred, with confidence)
    C: Contextual (from usage)
    D: Cross-layer (structural ↔ semantic)
    """

    # A: Structural explicit
    CONTAINS = "contains"  # Table → Column
    FOREIGN_KEY = "foreign_key"  # Column → Column (FK reference)
    DERIVED_FROM = "derived_from"  # Table/Column → Table/Column (ETL lineage)
    PARTITION_OF = "partition_of"  # Table → Table (time partition)
    SCHEMA_SAME = "schema_same"  # Table → Table (identical structure)

    # B: Semantic implicit (inferred)
    SEMANTIC_SYNONYM = "semantic_synonym"  # Column → Column (name semantics ≡)
    SEMANTIC_TYPE_MATCH = "semantic_type_match"  # Column → Column (same semantic type)
    JOINABLE = "joinable"  # Column → Column (value domain overlap)
    CORRELATED = "correlated"  # Column → Column (statistical correlation)
    DISTRIBUTION_SIMILAR = "distribution_similar"  # Column → Column (similar distribution)

    # C: Contextual (from usage — future)
    CO_OCCURS_IN_QUERY = "co_occurs_in_query"  # Column → Column (same SQL)
    FREQUENTLY_JOINED = "frequently_joined"  # Table → Table (frequent JOIN)
    USED_IN_DASHBOARD = "used_in_dashboard"  # Column → Dashboard

    # D: Cross-layer
    REPRESENTS = "represents"  # Structural node → Semantic node (Column → Concept)
    HAS_CONCEPT = "has_concept"  # Entity → Concept (entity has this concept)


class Edge(BaseModel):
    """An edge connecting two nodes with a relationship type and confidence."""

    id: str = Field(description="Unique identifier for the edge")
    source_id: str = Field(description="ID of the source node")
    target_id: str = Field(description="ID of the target node")
    type: EdgeType = Field(description="Type of the relationship")
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence score for inferred edges (0.0-1.0), 1.0 for explicit edges",
    )
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional properties (e.g., FK constraint name, correlation coefficient)",
    )

    @property
    def is_explicit(self) -> bool:
        """Whether this edge comes from explicit schema/metadata (confidence=1.0)."""
        return self.confidence == 1.0

    @property
    def is_inferred(self) -> bool:
        """Whether this edge was inferred (confidence < 1.0)."""
        return self.confidence < 1.0


class PendingEdge(BaseModel):
    """An edge whose source/target node(s) don't yet exist in the graph.

    Typically created when a FK references a table/column from a datasource
    that hasn't been added yet. When that datasource is later added via
    add_table, the Pipeline resolves pending edges — moving them into the
    main edges table so they participate in retrieval and traversal.
    """

    id: str = Field(description="Unique identifier for the pending edge")
    source_id: str = Field(description="ID of the source node (may not exist yet)")
    target_id: str = Field(description="ID of the target node (may not exist yet)")
    type: EdgeType = Field(description="Type of the relationship")
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence score (0.0-1.0), 1.0 for explicit edges like FK",
    )
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional properties (e.g., FK constraint name)",
    )
    missing_endpoints: list[str] = Field(
        default_factory=list,
        description="Which endpoint IDs are missing: 'source', 'target', or both",
    )
