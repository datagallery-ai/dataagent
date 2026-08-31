"""Column profile model — statistical fingerprint of a data column."""

from typing import Any

from pydantic import BaseModel, Field


class ColumnProfile(BaseModel):
    """Statistical fingerprint of a column, computed from sampled data.

    This profile captures the essential characteristics of a column's data
    distribution, enabling semantic type classification and relationship inference.
    """

    id: str = Field(description="Unique identifier for this profile")
    column_id: str = Field(description="ID of the ColumnNode this profile belongs to")

    # Type information
    dtype: str = Field(description="Detected data type (e.g., integer, float, string, datetime)")
    semantic_type: str = Field(
        default="unknown",
        description="Pre-classified semantic type (e.g., email_address, monetary_value, identifier)",
    )

    # Basic statistics
    null_rate: float = Field(default=0.0, ge=0.0, le=1.0, description="Fraction of null values")
    cardinality: int = Field(default=0, description="Number of distinct values")
    unique_rate: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Fraction of unique values (cardinality / total)"
    )
    total_count: int = Field(default=0, description="Total number of values (including nulls)")

    # Numeric statistics (only populated for numeric columns)
    min_value: float | None = Field(default=None, description="Minimum value")
    max_value: float | None = Field(default=None, description="Maximum value")
    mean_value: float | None = Field(default=None, description="Mean value")
    std_value: float | None = Field(default=None, description="Standard deviation")
    median_value: float | None = Field(default=None, description="Median value")

    # String statistics (only populated for string columns)
    min_length: int | None = Field(default=None, description="Minimum string length")
    max_length: int | None = Field(default=None, description="Maximum string length")
    avg_length: float | None = Field(default=None, description="Average string length")

    # Value patterns and frequencies
    top_values: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Top-N most frequent values: [{'value': x, 'count': n, 'fraction': f}]",
    )
    value_patterns: list[str] = Field(
        default_factory=list,
        description="Detected regex patterns (e.g., email pattern, phone pattern)",
    )

    # Distribution
    distribution_histogram: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Distribution histogram: numeric → binned [{'bin': x, 'count': n}], "
        "categorical → frequency [{'value': x, 'count': n}]",
    )

    # Sample values for LLM context
    sample_values: list[Any] = Field(
        default_factory=list,
        description="A small set of representative sample values for semantic mapping",
    )
