"""File connector for CSV and Parquet data sources."""

import logging
from pathlib import Path
from typing import Any

import pandas as pd

from datalink.connector.base import BaseConnector
from datalink.models.datasource import (
    ColumnInfo,
    DatasourceConfig,
    DatasourceInfo,
    TableInfo,
)

logger = logging.getLogger(__name__)


class FileConnector(BaseConnector):
    """Connector for local CSV and Parquet files.

    Treats each file as a table. Extracts schema by reading the file
    and inferring types from the data. No foreign key information
    is available from flat files — that must be inferred later.
    """

    def __init__(self, config: DatasourceConfig):
        super().__init__(config)
        self._dataframes: dict[str, pd.DataFrame] = {}

    def connect(self) -> None:
        """Read all files from the specified path."""
        path = Path(self.config.path)
        if not path.exists():
            raise ValueError(f"Path does not exist: {path}")

        if path.is_file():
            self._read_single_file(path)
        elif path.is_dir():
            self._read_directory(path)
        else:
            logger.warning(f"Path is neither file nor directory: {path}")
            return

    def get_datasource_info(self) -> DatasourceInfo:
        """Extract schema metadata and sample data from loaded files."""
        if not self._dataframes:
            self.connect()

        tables: list[TableInfo] = []
        sample_data: dict[str, list[dict[str, Any]]] = {}

        for name, df in self._dataframes.items():
            columns = self._infer_columns(df)
            table_info = TableInfo(
                name=name,
                schema_name="file",
                columns=columns,
                foreign_keys=[],  # No FK info from flat files
                row_count=len(df),
                source=str(self.config.path),
            )
            tables.append(table_info)

            # Convert sample data to list of dicts
            sample_df = df.head(self.config.sample_size)
            sample_data[name] = sample_df.to_dict(orient="records")

        return DatasourceInfo(
            config=self.config,
            tables=tables,
            sample_data=sample_data,
        )

    def get_sample_data(self, table_name: str, n: int = 1000) -> list[dict[str, Any]]:
        """Get sample rows from a loaded DataFrame."""
        if table_name not in self._dataframes:
            logger.warning(f"Table '{table_name}' not found in loaded data")
            return []
        df = self._dataframes[table_name]
        sample_df = df.head(n)
        return sample_df.to_dict(orient="records")

    def disconnect(self) -> None:
        """Clear loaded dataframes."""
        self._dataframes.clear()

    def _read_single_file(self, path: Path) -> None:
        """Read a single CSV or Parquet file."""
        name = path.stem  # filename without extension
        df = self._load_file(path)
        self._dataframes[name] = df
        logger.info(f"Loaded file '{path.name}' as table '{name}' ({len(df)} rows)")

    def _read_directory(self, path: Path) -> None:
        """Read all CSV and Parquet files in a directory."""
        csv_files = list(path.glob("*.csv"))
        parquet_files = list(path.glob("*.parquet")) + list(path.glob("*.pq"))

        if not csv_files and not parquet_files:
            logger.warning(f"No CSV or Parquet files found in: {path}")
            return

        for f in csv_files + parquet_files:
            name = f.stem
            df = self._load_file(f)
            self._dataframes[name] = df
            logger.info(f"Loaded '{f.name}' as table '{name}' ({len(df)} rows)")

    def _load_file(self, path: Path) -> pd.DataFrame:
        """Load a file into a DataFrame based on its extension."""
        ext = path.suffix.lower()
        if ext == ".csv":
            return pd.read_csv(path)
        elif ext in (".parquet", ".pq"):
            return pd.read_parquet(path)
        else:
            raise ValueError(f"Unsupported file format: {ext}")

    def _infer_columns(self, df: pd.DataFrame) -> list[ColumnInfo]:
        """Infer column metadata from a DataFrame."""
        columns = []
        for col_name in df.columns:
            dtype_str = self._classify_dtype(df[col_name])
            nullable = df[col_name].isna().any()
            unique_count = df[col_name].nunique()
            total_count = len(df)

            columns.append(
                ColumnInfo(
                    name=col_name,
                    dtype=dtype_str,
                    nullable=nullable,
                    is_primary_key=unique_count == total_count and not nullable,
                    comment="",  # No comments from flat files
                )
            )
        return columns

    def _classify_dtype(self, series: pd.Series) -> str:
        """Classify a pandas Series dtype into a more descriptive type string."""
        if pd.api.types.is_integer_dtype(series):
            return "integer"
        elif pd.api.types.is_float_dtype(series):
            return "float"
        elif pd.api.types.is_bool_dtype(series):
            return "boolean"
        elif pd.api.types.is_datetime64_any_dtype(series):
            return "datetime"
        elif pd.api.types.is_string_dtype(series) or pd.api.types.is_object_dtype(series):
            # Try to detect if it's actually numeric strings or datetime strings
            non_null = series.dropna()
            if len(non_null) > 0:
                try:
                    pd.to_numeric(non_null)
                    return "numeric_string"
                except (ValueError, TypeError):
                    pass
                try:
                    dt = pd.to_datetime(non_null, errors="coerce", format="mixed")
                    if dt.notna().sum() / len(non_null) > 0.9:
                        return "datetime_string"
                except (ValueError, TypeError):
                    pass
            return "string"
        else:
            return str(series.dtype)
