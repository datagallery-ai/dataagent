"""JoinableInferrer — detect columns that can be joined by value domain overlap."""

import logging

from datalink.models.edge import Edge, EdgeType
from datalink.models.node import ColumnNode
from datalink.models.profile import ColumnProfile

logger = logging.getLogger(__name__)


class JoinableInferrer:
    """Detect joinable relationships between columns based on value overlap.

    Two columns are joinable if their value domains have significant overlap.
    This is computed by comparing the intersection of top-N frequent values
    and sample intersection rates.

    Boolean-type columns (boolean, integer_boolean, float_boolean) are excluded
    because their tiny value domains (e.g. {0, 1}) trivially overlap with any
    other boolean column, producing meaningless joinable edges.
    """

    def __init__(self, overlap_threshold: float = 0.1, max_cardinality: int = 900):
        """Initialize the joinable inferrer.

        Args:
            overlap_threshold: Minimum overlap rate to consider columns joinable.
            max_cardinality: Skip columns with cardinality above this threshold
                            (too many unique values make sampling unreliable).
        """
        self.overlap_threshold = overlap_threshold
        self.max_cardinality = max_cardinality

    def infer(
        self,
        columns: list[ColumnNode],
        profiles: list[ColumnProfile],
    ) -> list[Edge]:
        """Find joinable relationships between columns across different tables.

        Args:
            columns: All column nodes to compare.
            profiles: Corresponding column profiles with top_values and samples.

        Returns:
            List of joinable edges with confidence scores.
        """
        edges: list[Edge] = []

        # Build lookup: column_id -> profile
        profile_map = {p.column_id: p for p in profiles}

        # Only consider columns from different tables and compatible types
        # Group columns by table
        table_groups: dict[str, list[ColumnNode]] = {}
        for col in columns:
            if col.table_id not in table_groups:
                table_groups[col.table_id] = []
            table_groups[col.table_id].append(col)

        tables = list(table_groups.keys())

        # Compare columns across different table pairs
        for i, table_a in enumerate(tables):
            for table_b in tables[i + 1 :]:
                for col_a in table_groups[table_a]:
                    profile_a = profile_map.get(col_a.id)
                    if profile_a is None or profile_a.cardinality > self.max_cardinality:
                        continue
                    # Skip boolean-type columns — their tiny value domains
                    # trivially overlap, producing meaningless joinable edges
                    if self._is_boolean_skip(profile_a):
                        continue

                    for col_b in table_groups[table_b]:
                        profile_b = profile_map.get(col_b.id)
                        if profile_b is None or profile_b.cardinality > self.max_cardinality:
                            continue
                        if self._is_boolean_skip(profile_b):
                            continue

                        # Skip incompatible dtype pairs (string vs integer is allowed
                        # since identifiers can be stored as either)
                        if not self._compatible_dtypes(profile_a.dtype, profile_b.dtype):
                            continue

                        # Compute overlap rate
                        overlap_rate = self._compute_overlap(profile_a, profile_b)
                        if overlap_rate >= self.overlap_threshold:
                            edge = Edge(
                                id=f"edge:joinable:{col_a.id}:{col_b.id}",
                                source_id=col_a.id,
                                target_id=col_b.id,
                                type=EdgeType.JOINABLE,
                                confidence=overlap_rate,
                                properties={
                                    "overlap_rate": overlap_rate,
                                    "dtype_a": profile_a.dtype,
                                    "dtype_b": profile_b.dtype,
                                },
                            )
                            edges.append(edge)
                            logger.debug(f"Found joinable: {col_a.name} <-> {col_b.name} (overlap={overlap_rate:.3f})")

        logger.info(f"Found {len(edges)} joinable edges")
        return edges

    def _is_boolean_skip(self, profile: ColumnProfile) -> bool:
        """Check if a column is a boolean-type column that should be skipped.

        Boolean columns (including integer_boolean and float_boolean detected
        by the profiler) have tiny value domains that trivially overlap with
        any other boolean column, producing spurious joinable edges.

        Examples of skipped columns:
        - boolean (pandas bool dtype)
        - integer_boolean (BIGINT/INTEGER storing only {0, 1})
        - float_boolean (FLOAT storing only {0.0, 1.0})
        """
        return profile.dtype in ("boolean", "integer_boolean", "float_boolean")

    def _compatible_dtypes(self, dtype_a: str, dtype_b: str) -> bool:
        """Check if two dtypes can potentially be compared for joinability."""
        numeric_types = {"integer", "float", "integer_boolean", "float_boolean"}
        string_types = {"string"}
        temporal_types = {"datetime", "date"}

        # Numeric types are compatible with each other
        if dtype_a in numeric_types and dtype_b in numeric_types:
            return True
        # String types are compatible with each other
        if dtype_a in string_types and dtype_b in string_types:
            return True
        # Temporal types are compatible with each other
        if dtype_a in temporal_types and dtype_b in temporal_types:
            return True
        # Identifiers can be stored as integer or string
        if (dtype_a in numeric_types and dtype_b in string_types) or (
            dtype_a in string_types and dtype_b in numeric_types
        ):
            return True

        return False

    def _compute_overlap(self, profile_a: ColumnProfile, profile_b: ColumnProfile) -> float:
        """Compute value overlap rate between two column profiles.

        Uses top values and sample values to estimate the fraction of values
        in column A that appear in column B's domain.
        """
        # Extract value sets from top_values
        values_a = {item["value"] for item in profile_a.top_values}
        values_b = {item["value"] for item in profile_b.top_values}

        # Also include sample values
        if profile_a.sample_values:
            values_a.update(str(v) for v in profile_a.sample_values)
        if profile_b.sample_values:
            values_b.update(str(v) for v in profile_b.sample_values)

        if not values_a or not values_b:
            return 0.0

        # Compute overlap: fraction of A's values that appear in B
        intersection = values_a & values_b
        overlap_rate = len(intersection) / min(len(values_a), len(values_b))

        return overlap_rate
