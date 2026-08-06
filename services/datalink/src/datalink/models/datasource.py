"""Datasource configuration and extraction result models."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DatasourceType(str, Enum):
    """Types of supported data sources."""

    DATABASE = "database"  # PostgreSQL, MySQL, SQLite, etc.
    CSV = "csv"  # Local CSV files
    PARQUET = "parquet"  # Local Parquet files


class DatasourceConfig(BaseModel):
    """Configuration for connecting to a data source."""

    type: DatasourceType = Field(description="Type of the data source")
    # For database sources
    connection_string: str = Field(
        default="", description="SQLAlchemy connection string (e.g., postgresql://user:pass@host/db)"
    )
    # For file sources
    path: str = Field(default="", description="Path to file or directory (for CSV/Parquet)")
    # Schema name for database sources (optional)
    schema_name: str = Field(default="public", description="Database schema to inspect")

    # Sampling configuration
    sample_size: int = Field(default=1000, description="Number of rows to sample for profiling")


class ForeignKeyInfo(BaseModel):
    """Foreign key constraint between two columns."""

    constraint_name: str = Field(default="", description="Name of the FK constraint")
    source_table: str = Field(description="Table containing the FK column")
    source_column: str = Field(description="FK column name")
    target_table: str = Field(description="Referenced table")
    target_column: str = Field(description="Referenced column")


class ColumnInfo(BaseModel):
    """Raw column metadata extracted from a data source."""

    name: str = Field(description="Column name")
    dtype: str = Field(description="Data type as reported by the source")
    nullable: bool = Field(default=True, description="Whether the column allows nulls")
    is_primary_key: bool = Field(default=False, description="Whether this column is a primary key")
    comment: str = Field(default="", description="Comment/description from metadata")
    default_value: Any = Field(default=None, description="Default value for the column")


class TableInfo(BaseModel):
    """Raw table metadata extracted from a data source."""

    name: str = Field(description="Table name")
    schema_name: str | None = Field(
        default="public",
        description="Schema/database name. None for MySQL/MariaDB where the connected database IS the schema.",
    )
    columns: list[ColumnInfo] = Field(default_factory=list, description="All columns in this table")
    foreign_keys: list[ForeignKeyInfo] = Field(default_factory=list, description="FK constraints involving this table")
    row_count: int | None = Field(default=None, description="Number of rows (None if not available from metadata)")
    comment: str = Field(default="", description="Table-level comment/description")
    source: str = Field(default="", description="Source identifier this table came from")


class DatasourceInfo(BaseModel):
    """Complete extraction result from a data source."""

    config: DatasourceConfig = Field(description="The config used to connect")
    tables: list[TableInfo] = Field(default_factory=list, description="All tables found in this datasource")
    # Sample data keyed by table name
    sample_data: dict[str, list[dict[str, Any]]] = Field(
        default_factory=dict,
        description="Sample rows for each table: {table_name: [{col1: val1, ...}, ...]}",
    )
