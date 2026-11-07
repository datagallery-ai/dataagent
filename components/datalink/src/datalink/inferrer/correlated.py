"""CorrelationInferrer — detect statistical correlations between numeric columns.

The key insight: correlation only makes sense between columns from *different*
tables, after aligning rows via a join key. So for each joinable edge (a1 ↔ b1),
we find other numeric columns in the same tables (a2, a3, … and b2, b3, …),
merge the sample data on a1 = b1, then compute Pearson correlation on the
aligned (a2, b2) pairs.
"""

import logging

import pandas as pd

from datalink.models.datasource import DatasourceInfo
from datalink.models.edge import Edge, EdgeType
from datalink.models.node import ColumnNode
from datalink.models.profile import ColumnProfile
from datalink.utils.ids import parse_column_id

logger = logging.getLogger(__name__)

NUMERIC_DTYPES = {"integer", "float"}

# Minimum number of aligned rows required to compute a meaningful Pearson r
MIN_ALIGNED_ROWS = 5


class CorrelationInferrer:
    """Detect statistical correlations between numeric columns across tables.

    For each pair of tables connected by a joinable edge (join key), find
    other numeric columns in each table, merge sample data on the join key,
    and compute Pearson correlation on the aligned values.

    The resulting correlated edges connect the non-key numeric columns
    (e.g., orders.amount ↔ users.age), not the join keys themselves.
    """

    def __init__(self, correlation_threshold: float = 0.5):
        """Initialize the correlation inferrer.

        Args:
            correlation_threshold: Minimum absolute Pearson correlation
                                  coefficient to create an edge.
        """
        self.correlation_threshold = correlation_threshold

    def infer(
        self,
        columns: list[ColumnNode],
        profiles: list[ColumnProfile],
        joinable_edges: list[Edge],
        datasource_infos: list[DatasourceInfo],
    ) -> list[Edge]:
        """Find correlated relationships between numeric columns across tables.

        For each joinable edge (key_a ↔ key_b), we:
        1. Find all numeric columns in key_a's table (excluding key_a itself)
        2. Find all numeric columns in key_b's table (excluding key_b itself)
        3. Merge sample data from both tables on key_a = key_b
        4. Compute Pearson correlation for each (numeric_a, numeric_b) pair
        5. If |r| >= threshold, create a correlated edge between those columns

        Args:
            columns: All column nodes.
            profiles: Corresponding column profiles.
            joinable_edges: Pre-computed joinable edges (these define the join keys).
            datasource_infos: Source data containing sample_data for merging.

        Returns:
            List of correlated edges (connecting non-key numeric columns).
        """
        edges: list[Edge] = []

        # Build lookups
        profile_map = {p.column_id: p for p in profiles}
        col_map = {c.id: c for c in columns}

        # Group columns by table_id for fast lookup
        table_columns: dict[str, list[ColumnNode]] = {}
        for col in columns:
            if col.table_id not in table_columns:
                table_columns[col.table_id] = []
            table_columns[col.table_id].append(col)

        # Build sample data lookup: table_name -> DataFrame
        # Combine sample_data from all datasource_infos
        sample_dfs: dict[str, pd.DataFrame] = {}
        for ds_info in datasource_infos:
            for table_name, rows in ds_info.sample_data.items():
                if rows:
                    sample_dfs[table_name] = pd.DataFrame(rows)

        # For each joinable edge, find correlated non-key numeric columns
        for joinable_edge in joinable_edges:
            key_a_id = joinable_edge.source_id
            key_b_id = joinable_edge.target_id

            key_a = col_map.get(key_a_id)
            key_b = col_map.get(key_b_id)

            if key_a is None or key_b is None:
                continue

            # Resolve table/column names without naive split(":") —
            # source URLs (sqlite:///..., postgresql://...) contain colons.
            parsed_a = parse_column_id(key_a_id)
            parsed_b = parse_column_id(key_b_id)
            if parsed_a is None or parsed_b is None:
                continue

            table_name_a = parsed_a.table_name
            table_name_b = parsed_b.table_name
            col_name_a = key_a.name
            col_name_b = key_b.name

            df_a = sample_dfs.get(table_name_a)
            df_b = sample_dfs.get(table_name_b)

            if df_a is None or df_b is None:
                logger.debug(
                    f"Skipping correlation for {key_a_id} ↔ {key_b_id}: "
                    f"no sample data for {table_name_a} or {table_name_b}"
                )
                continue

            # Ensure join key columns exist in the DataFrames
            if col_name_a not in df_a.columns or col_name_b not in df_b.columns:
                continue

            # Coerce join key columns to the same dtype before merging.
            # Cross-table join keys may have mismatched types (e.g., int vs str)
            # which causes pd.merge to raise a TypeError. Convert both to str
            # so the merge always succeeds regardless of source dtype.
            df_a = df_a.copy()
            df_b = df_b.copy()
            df_a[col_name_a] = df_a[col_name_a].astype(str)
            df_b[col_name_b] = df_b[col_name_b].astype(str)

            # Merge sample data on the join key
            merged = pd.merge(
                df_a,
                df_b,
                left_on=col_name_a,
                right_on=col_name_b,
                how="inner",
                suffixes=("_a", "_b"),
            )

            if len(merged) < MIN_ALIGNED_ROWS:
                logger.debug(
                    f"Skipping correlation for {key_a_id} ↔ {key_b_id}: "
                    f"only {len(merged)} aligned rows (need {MIN_ALIGNED_ROWS})"
                )
                continue

            # Find other numeric columns in each table (excluding the join key)
            numeric_cols_a = self._find_numeric_columns(
                key_a.table_id,
                col_name_a,
                profile_map,
                table_columns,
                merged,
                suffix="_a",
            )
            numeric_cols_b = self._find_numeric_columns(
                key_b.table_id,
                col_name_b,
                profile_map,
                table_columns,
                merged,
                suffix="_b",
            )

            if not numeric_cols_a or not numeric_cols_b:
                continue

            # Compute Pearson correlation for each pair
            for (col_a_id, col_a_name_df), (col_b_id, col_b_name_df) in self._pair_numeric_columns(
                numeric_cols_a, numeric_cols_b, key_a.table_id, key_b.table_id
            ):
                corr = self._compute_pearson(merged, col_a_name_df, col_b_name_df)
                if corr is not None and abs(corr) >= self.correlation_threshold:
                    edge = Edge(
                        id=f"edge:correlated:{col_a_id}:{col_b_id}",
                        source_id=col_a_id,
                        target_id=col_b_id,
                        type=EdgeType.CORRELATED,
                        confidence=abs(corr),
                        properties={
                            "coefficient": corr,
                            "method": "pearson",
                            "aligned_rows": len(merged),
                            "joinable_edge": joinable_edge.id,
                            "join_key_a": key_a_id,
                            "join_key_b": key_b_id,
                        },
                    )
                    edges.append(edge)
                    logger.debug(
                        f"Found correlation: {col_a_id} ↔ {col_b_id} "
                        f"(r={corr:.3f}, aligned_rows={len(merged)}, "
                        f"join_key={key_a_id}↔{key_b_id})"
                    )

        logger.info(f"Found {len(edges)} correlated edges")
        return edges

    def _find_numeric_columns(
        self,
        table_id: str,
        exclude_col_name: str,
        profile_map: dict[str, ColumnProfile],
        table_columns: dict[str, list[ColumnNode]],
        merged: pd.DataFrame,
        suffix: str,
    ) -> list[tuple[str, str]]:
        """Find numeric columns in a table that exist in the merged DataFrame.

        After a pd.merge with suffixes=("_a", "_b"), column names that clash
        between the two input DataFrames get a suffix appended (e.g., "col_a").
        Column names that are unique keep their original name. So we need to
        check both the suffixed name and the original name.

        Returns list of (column_id, merged_df_column_name) tuples.
        Excludes the join key column.
        """
        result = []
        cols_in_table = table_columns.get(table_id, [])

        for col in cols_in_table:
            col_name = col.name

            # Skip the join key
            if col_name == exclude_col_name:
                continue

            # Check dtype is numeric
            profile = profile_map.get(col.id)
            if profile is None or profile.dtype not in NUMERIC_DTYPES:
                continue

            # Find the column in the merged DataFrame.
            # pandas merge applies suffix only to clashing column names;
            # unique names keep their original name.
            suffixed_name = f"{col_name}{suffix}"
            if suffixed_name in merged.columns:
                merged_name = suffixed_name
            elif col_name in merged.columns:
                merged_name = col_name
            else:
                continue

            # Verify it's actually numeric in the merged DataFrame
            if pd.api.types.is_numeric_dtype(merged[merged_name]):
                result.append((col.id, merged_name))

        return result

    def _pair_numeric_columns(
        self,
        cols_a: list[tuple[str, str]],
        cols_b: list[tuple[str, str]],
        table_id_a: str,
        table_id_b: str,
    ) -> list[tuple[tuple[str, str], tuple[str, str]]]:
        """Generate pairs of numeric columns from different tables.

        Only pairs columns from different tables (which is guaranteed here
        since cols_a and cols_b come from different tables).
        """
        pairs = []
        for col_a in cols_a:
            for col_b in cols_b:
                pairs.append((col_a, col_b))
        return pairs

    def _compute_pearson(
        self,
        merged: pd.DataFrame,
        col_name_a: str,
        col_name_b: str,
    ) -> float | None:
        """Compute Pearson correlation between two columns in the merged DataFrame.

        Args:
            merged: DataFrame with aligned rows from both tables.
            col_name_a: Column name in merged DataFrame (with suffix).
            col_name_b: Column name in merged DataFrame (with suffix).

        Returns:
            Pearson r value, or None if computation fails or insufficient data.
        """
        try:
            series_a = pd.to_numeric(merged[col_name_a], errors="coerce").dropna()
            series_b = pd.to_numeric(merged[col_name_b], errors="coerce").dropna()

            # Align indices after dropna
            common_idx = series_a.index.intersection(series_b.index)
            if len(common_idx) < MIN_ALIGNED_ROWS:
                return None

            series_a = series_a.loc[common_idx]
            series_b = series_b.loc[common_idx]

            corr = series_a.corr(series_b, method="pearson")

            if pd.isna(corr):
                return None

            return float(corr)
        except Exception:
            return None
