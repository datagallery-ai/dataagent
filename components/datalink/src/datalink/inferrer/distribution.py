"""DistributionInferrer — detect columns with similar data distributions."""

import logging

from datalink.models.edge import Edge, EdgeType
from datalink.models.node import ColumnNode
from datalink.models.profile import ColumnProfile

logger = logging.getLogger(__name__)


class DistributionInferrer:
    """Detect distribution similarity between columns.

    Useful for detecting columns with similar time ranges (e.g., order_date
    and tx_timestamp) or similar categorical distributions.
    """

    def __init__(self, similarity_threshold: float = 0.5):
        """Initialize the distribution inferrer.

        Args:
            similarity_threshold: Minimum similarity score to create an edge.
        """
        self.similarity_threshold = similarity_threshold

    def infer(
        self,
        columns: list[ColumnNode],
        profiles: list[ColumnProfile],
    ) -> list[Edge]:
        """Find distribution similarity relationships.

        Args:
            columns: All column nodes to compare.
            profiles: Corresponding column profiles.

        Returns:
            List of distribution_similar edges.
        """
        edges: list[Edge] = []
        profile_map = {p.column_id: p for p in profiles}

        for i, col_a in enumerate(columns):
            for col_b in columns[i + 1 :]:
                # Skip same-table comparisons
                if col_a.table_id == col_b.table_id:
                    continue

                profile_a = profile_map.get(col_a.id)
                profile_b = profile_map.get(col_b.id)
                if profile_a is None or profile_b is None:
                    continue

                # Only compare columns of similar types
                if profile_a.dtype != profile_b.dtype:
                    continue

                similarity = self._compute_similarity(profile_a, profile_b)
                if similarity >= self.similarity_threshold:
                    edge = Edge(
                        id=f"edge:dist_similar:{col_a.id}:{col_b.id}",
                        source_id=col_a.id,
                        target_id=col_b.id,
                        type=EdgeType.DISTRIBUTION_SIMILAR,
                        confidence=similarity,
                        properties={
                            "similarity_score": similarity,
                            "dtype": profile_a.dtype,
                        },
                    )
                    edges.append(edge)
                    logger.debug(
                        f"Found distribution similarity: {col_a.name} <-> {col_b.name} (score={similarity:.3f})"
                    )

        logger.info(f"Found {len(edges)} distribution similar edges")
        return edges

    def _compute_similarity(self, profile_a: ColumnProfile, profile_b: ColumnProfile) -> float:
        """Compute distribution similarity between two column profiles.

        For numeric columns: compare value ranges and histogram shapes.
        For string columns: compare top value overlap.
        """
        # Numeric comparison
        numeric_types = {"integer", "float"}
        if profile_a.dtype in numeric_types and profile_b.dtype in numeric_types:
            return self._numeric_similarity(profile_a, profile_b)

        # String/categorical comparison
        if profile_a.dtype == "string" and profile_b.dtype == "string":
            return self._categorical_similarity(profile_a, profile_b)

        # Temporal comparison
        temporal_types = {"datetime", "date"}
        if profile_a.dtype in temporal_types and profile_b.dtype in temporal_types:
            return self._temporal_similarity(profile_a, profile_b)

        return 0.0

    def _numeric_similarity(self, profile_a: ColumnProfile, profile_b: ColumnProfile) -> float:
        """Compare numeric distributions using range overlap and histogram similarity."""
        # Range overlap: how much of the value ranges overlap
        if profile_a.min_value is None or profile_a.max_value is None:
            return 0.0
        if profile_b.min_value is None or profile_b.max_value is None:
            return 0.0

        range_a = (profile_a.min_value, profile_a.max_value)
        range_b = (profile_b.min_value, profile_b.max_value)

        # Compute overlap fraction
        overlap_start = max(range_a[0], range_b[0])
        overlap_end = min(range_a[1], range_b[1])

        if overlap_start > overlap_end:
            return 0.0  # No overlap at all

        overlap_range = overlap_end - overlap_start
        total_range = max(range_a[1], range_b[1]) - min(range_a[0], range_b[0])

        if total_range == 0:
            return 1.0  # Both are constant values at the same point

        overlap_fraction = overlap_range / total_range

        # Compare mean/std similarity
        mean_similarity = 0.0
        if profile_a.mean_value is not None and profile_b.mean_value is not None:
            mean_diff = abs(profile_a.mean_value - profile_b.mean_value)
            max_mean = max(abs(profile_a.mean_value), abs(profile_b.mean_value))
            if max_mean > 0:
                mean_similarity = 1.0 - mean_diff / max_mean

        return overlap_fraction * 0.6 + mean_similarity * 0.4

    def _categorical_similarity(self, profile_a: ColumnProfile, profile_b: ColumnProfile) -> float:
        """Compare categorical distributions using top value overlap."""
        values_a = {item["value"]: item["fraction"] for item in profile_a.top_values}
        values_b = {item["value"]: item["fraction"] for item in profile_b.top_values}

        common_values = set(values_a.keys()) & set(values_b.keys())
        if not common_values:
            return 0.0

        # Compute weighted overlap (sum of min fractions for common values)
        weighted_overlap = sum(min(values_a[v], values_b[v]) for v in common_values)
        total_weight = sum(values_a.values()) + sum(values_b.values())

        if total_weight == 0:
            return 0.0

        return weighted_overlap / (total_weight / 2)

    def _temporal_similarity(self, profile_a: ColumnProfile, profile_b: ColumnProfile) -> float:
        """Compare temporal distributions using histogram overlap."""
        if not profile_a.distribution_histogram or not profile_b.distribution_histogram:
            return 0.0

        # Simple comparison: both have temporal data
        # For MVP, just check if they have similar number of bins and data density
        len_a = len(profile_a.distribution_histogram)
        len_b = len(profile_b.distribution_histogram)

        if len_a == 0 or len_b == 0:
            return 0.0

        # Similar bin count suggests similar time range coverage
        bin_similarity = min(len_a, len_b) / max(len_a, len_b)

        return bin_similarity * 0.5  # Conservative estimate
