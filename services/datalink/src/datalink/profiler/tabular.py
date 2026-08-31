"""Profile tabular data columns — compute statistical fingerprints."""

import logging
import re
from collections import Counter
from typing import Any

import numpy as np
import pandas as pd

from datalink.models.datasource import DatasourceInfo
from datalink.models.profile import ColumnProfile

logger = logging.getLogger(__name__)

# Semantic type classification rules: pattern -> semantic_type
SEMANTIC_TYPE_RULES = [
    (r"^[\w.+-]+@[\w-]+\.[\w.-]+$", "email_address"),
    (r"^\+?\d{1,3}[-.\s]?\(?\d{1,4}\)?[-.\s]?\d{1,4}[-.\s]?\d{1,9}$", "phone_number"),
    (r"^https?://[\w./-]+$", "url"),
    (r"^\d{4}-\d{2}-\d{2}$", "date_iso"),
    (r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", "datetime_iso"),
    (r"^[A-Z]{2,3}[-\d]+$", "identifier_code"),
    (r"^\d+\.\d{2}$", "monetary_value"),
    (r"^[\w-]+_id$", "identifier"),
    (r"^id$", "identifier"),
    (r".*_id$", "identifier"),
    (r".*_name$", "person_name"),
    (r"^name$", "person_name"),
    (r".*_date$", "timestamp"),
    (r".*_time$", "timestamp"),
    (r".*_amount$", "monetary_value"),
    (r"^amount$", "monetary_value"),
    (r"^value$", "monetary_value"),
    (r".*_price$", "monetary_value"),
    (r".*_cost$", "monetary_value"),
    (r".*_rate$", "rate_percentage"),
    (r".*_count$", "count"),
    (r".*_percent", "rate_percentage"),
    (r".*_status$", "status_enum"),
    (r"^status$", "status_enum"),
    (r".*_type$", "type_enum"),
    (r".*_category$", "category_enum"),
    (r".*_email$", "email_address"),
    (r".*_phone$", "phone_number"),
    (r".*_address$", "physical_address"),
    (r".*_city$", "city_name"),
    (r".*_country$", "country_name"),
    (r".*_zip$", "postal_code"),
    (r".*_url$", "url"),
]


class TabularProfiler:
    """Compute ColumnProfile for each column in tabular data."""

    def profile_datasource(self, datasource_info: DatasourceInfo) -> list[ColumnProfile]:
        """Profile all columns across all tables in a datasource.

        Args:
            datasource_info: Must have sample_data populated.

        Returns:
            List of ColumnProfile objects for all columns.
        """
        profiles = []

        for table_info in datasource_info.tables:
            # Use the real path/connection_string from table_info.source
            # (same as TabularExtractor) for consistent node IDs
            source_name = (
                table_info.source
                or datasource_info.config.path
                or datasource_info.config.connection_string
                or "unknown"
            )
            table_name = table_info.name
            sample_rows = datasource_info.sample_data.get(table_name, [])

            if not sample_rows:
                logger.warning(f"No sample data for table '{table_name}', skipping profiling")
                continue

            # Convert to DataFrame for easier computation
            df = pd.DataFrame(sample_rows)

            for col_info in table_info.columns:
                col_name = col_info.name
                if col_name not in df.columns:
                    continue

                col_id = f"column:{source_name}:{table_name}:{col_name}"
                series = df[col_name]

                profile = self.profile_column(
                    column_id=col_id,
                    column_name=col_name,
                    series=series,
                    comment=col_info.comment,
                )
                profiles.append(profile)

        logger.info(f"Profiled {len(profiles)} columns")
        return profiles

    def profile_column(
        self,
        column_id: str,
        column_name: str,
        series: pd.Series,
        comment: str = "",
    ) -> ColumnProfile:
        """Compute a ColumnProfile for a single pandas Series.

        Args:
            column_id: ID of the ColumnNode this profile belongs to.
            column_name: Name of the column.
            series: The pandas Series containing the data.
            comment: Optional metadata comment about the column.

        Returns:
            A ColumnProfile with all statistical fingerprints.
        """
        non_null = series.dropna()
        total_count = len(series)
        null_count = series.isna().sum()
        null_rate = null_count / total_count if total_count > 0 else 0.0
        cardinality = non_null.nunique()
        unique_rate = cardinality / total_count if total_count > 0 else 0.0

        # Detect dtype
        dtype = self._classify_dtype(series)

        # Compute value patterns
        value_patterns = self._detect_patterns(non_null)

        # Classify semantic type
        semantic_type = self._classify_semantic_type(column_name, dtype, value_patterns, non_null)

        # Basic stats
        profile = ColumnProfile(
            id=f"profile:{column_id}",
            column_id=column_id,
            dtype=dtype,
            semantic_type=semantic_type,
            null_rate=null_rate,
            cardinality=cardinality,
            unique_rate=unique_rate,
            total_count=total_count,
            value_patterns=value_patterns,
        )

        # Numeric stats
        if pd.api.types.is_numeric_dtype(non_null):
            numeric_values = pd.to_numeric(non_null, errors="coerce").dropna()
            if len(numeric_values) > 0:
                profile.min_value = float(numeric_values.min())
                profile.max_value = float(numeric_values.max())
                profile.mean_value = float(numeric_values.mean())
                profile.std_value = float(numeric_values.std()) if len(numeric_values) > 1 else 0.0
                profile.median_value = float(numeric_values.median())
                profile.distribution_histogram = self._numeric_histogram(numeric_values)

        # String stats
        if dtype == "string" and len(non_null) > 0:
            str_values = non_null.astype(str)
            profile.min_length = int(str_values.str.len().min())
            profile.max_length = int(str_values.str.len().max())
            profile.avg_length = float(str_values.str.len().mean())

        # Top values
        profile.top_values = self._compute_top_values(non_null, top_n=10)

        # Sample values (for LLM context)
        if len(non_null) > 0:
            sample_size = min(5, len(non_null))
            profile.sample_values = non_null.sample(sample_size, random_state=42).tolist()

        return profile

    def _classify_dtype(self, series: pd.Series) -> str:
        """Classify the data type of a column.

        Detects boolean-like integer columns (BIGINT storing 0/1) by checking
        if an integer column's distinct values are only {0, 1} — these are
        classified as "integer_boolean" to distinguish them from genuine
        integer identifiers and enable semantic type detection downstream.
        """
        non_null = series.dropna()
        if len(non_null) == 0:
            return "unknown"

        if pd.api.types.is_bool_dtype(series):
            return "boolean"
        if pd.api.types.is_integer_dtype(series):
            # Check for boolean-like integers: BIGINT/INTEGER storing only 0/1
            distinct = non_null.nunique()
            if distinct <= 2:
                unique_vals = set(non_null.unique())
                if unique_vals <= {0, 1} or unique_vals <= {0} or unique_vals <= {1}:
                    return "integer_boolean"
            return "integer"
        if pd.api.types.is_float_dtype(series):
            # Check for boolean-like floats: FLOAT storing only 0.0/1.0
            distinct = non_null.nunique()
            if distinct <= 2:
                unique_vals = set(non_null.unique())
                if unique_vals <= {0.0, 1.0} or unique_vals <= {0.0} or unique_vals <= {1.0}:
                    return "float_boolean"
            return "float"
        if pd.api.types.is_datetime64_any_dtype(series):
            return "datetime"

        # Try numeric conversion for object dtype
        if pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series):
            try:
                numeric = pd.to_numeric(non_null, errors="coerce")
                if numeric.notna().sum() / len(non_null) > 0.9:
                    if numeric.mod(1).eq(0).all():
                        # Check for boolean-like integer stored as string
                        distinct = numeric.nunique()
                        if distinct <= 2:
                            unique_vals = set(numeric.dropna().unique())
                            if unique_vals <= {0, 1}:
                                return "integer_boolean"
                        return "integer"
                    return "float"
            except Exception:
                pass

            # Try datetime
            try:
                dt = pd.to_datetime(non_null, errors="coerce", format="mixed")
                if dt.notna().sum() / len(non_null) > 0.9:
                    return "datetime"
            except Exception:
                pass

            return "string"

        return "unknown"

    def _detect_patterns(self, series: pd.Series) -> list[str]:
        """Detect common regex patterns in string values."""
        if not (pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)):
            return []

        str_values = series.astype(str)
        if len(str_values) == 0:
            return []

        patterns_found = []
        sample_size = min(20, len(str_values))
        sample = str_values.sample(sample_size, random_state=42) if len(str_values) > sample_size else str_values

        # Check common patterns
        pattern_checks = [
            ("email_pattern", r"^[\w.+-]+@[\w-]+\.[\w.-]+$"),
            ("url_pattern", r"^https?://"),
            ("date_pattern", r"^\d{4}-\d{2}-\d{2}$"),
            ("datetime_pattern", r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"),
            ("uuid_pattern", r"^[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}$"),
            ("phone_pattern", r"^\+?\d[\d\s\-().]{7,}$"),
            ("zip_pattern", r"^\d{5}(-\d{4})?$"),
        ]

        for pattern_name, regex in pattern_checks:
            match_count = sum(1 for v in sample if re.match(regex, v))
            match_rate = match_count / len(sample)
            if match_rate > 0.8:
                patterns_found.append(pattern_name)

        return patterns_found

    def _classify_semantic_type(
        self,
        column_name: str,
        dtype: str,
        value_patterns: list[str],
        series: pd.Series,
    ) -> str:
        """Classify the semantic type of a column.

        Uses a combination of:
        1. Name-based rules (column name patterns)
        2. Value pattern detection (email, URL, etc.)
        3. Dtype heuristics
        """
        # Check value patterns first (most reliable)
        for pattern_name in value_patterns:
            pattern_to_semantic = {
                "email_pattern": "email_address",
                "url_pattern": "url",
                "date_pattern": "date",
                "datetime_pattern": "datetime",
                "uuid_pattern": "uuid",
                "phone_pattern": "phone_number",
                "zip_pattern": "postal_code",
            }
            if pattern_name in pattern_to_semantic:
                return pattern_to_semantic[pattern_name]

        # Check name-based rules
        col_lower = column_name.lower()
        for pattern, semantic_type in SEMANTIC_TYPE_RULES:
            if re.match(pattern, col_lower):
                return semantic_type

        # Dtype-based fallback
        if dtype in ("boolean", "integer_boolean", "float_boolean"):
            return "boolean_flag"
        if dtype == "datetime":
            return "timestamp"

        # Check if it looks like an identifier (high unique rate, integer-ish)
        if len(series) > 0:
            non_null = series.dropna()
            unique_rate = non_null.nunique() / len(series)
            if unique_rate > 0.9 and dtype in ("integer", "string"):
                return "identifier"

        return "unknown"

    def _compute_top_values(self, series: pd.Series, top_n: int = 10) -> list[dict[str, Any]]:
        """Compute the top-N most frequent values."""
        non_null = series.dropna()
        if len(non_null) == 0:
            return []

        counts = Counter(non_null.tolist())
        top = counts.most_common(top_n)
        total = len(non_null)

        return [{"value": str(val), "count": cnt, "fraction": cnt / total} for val, cnt in top]

    def _numeric_histogram(self, values: pd.Series, bins: int = 10) -> list[dict[str, Any]]:
        """Compute a binned histogram for numeric values."""
        if len(values) < 2:
            return []

        try:
            counts, bin_edges = np.histogram(values, bins=bins)
            result = []
            for i in range(len(counts)):
                result.append(
                    {
                        "bin_start": float(bin_edges[i]),
                        "bin_end": float(bin_edges[i + 1]),
                        "count": int(counts[i]),
                    }
                )
            return result
        except Exception:
            return []
