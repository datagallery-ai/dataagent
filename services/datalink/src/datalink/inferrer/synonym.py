"""SynonymInferrer — detect columns with similar semantic meaning."""

import logging

from datalink.models.edge import Edge, EdgeType
from datalink.models.node import ColumnNode
from datalink.models.profile import ColumnProfile

logger = logging.getLogger(__name__)


class SynonymInferrer:
    """Detect semantic synonym relationships between columns.

    Columns are semantic synonyms when they represent the same concept
    but may have different names (e.g., customer_id ≡ user_id ≡ client_id).

    Detection strategies:
    1. Same semantic_type from profiling → high confidence (0.8-0.9)
    2. Name similarity (string similarity) → medium confidence (0.5-0.7)
    3. Both conditions → very high confidence (0.95)
    """

    # Known synonym groups for common data concepts
    SYNONYM_GROUPS = [
        {"id", "identifier", "uid", "uuid", "guid"},
        {"customer_id", "user_id", "client_id", "account_id", "person_id"},
        {"name", "full_name", "person_name", "display_name"},
        {"email", "email_address", "mail", "email_address"},
        {"phone", "phone_number", "telephone", "tel", "mobile"},
        {"address", "street_address", "location", "physical_address"},
        {"amount", "value", "total", "sum", "price", "cost", "fee", "charge"},
        {"date", "timestamp", "created_at", "updated_at", "time", "datetime"},
        {"status", "state", "condition", "phase"},
        {"type", "category", "class", "kind", "group"},
        {"city", "city_name", "town", "municipality"},
        {"country", "country_name", "nation", "region"},
        {"age", "years", "year_old"},
        {"gender", "sex"},
        {"latitude", "lat", "y"},
        {"longitude", "lng", "lon", "x"},
        {"description", "desc", "notes", "comment", "details", "remarks"},
    ]

    def infer(
        self,
        columns: list[ColumnNode],
        profiles: list[ColumnProfile],
    ) -> list[Edge]:
        """Find semantic synonym relationships between columns.

        Args:
            columns: All column nodes to compare.
            profiles: Corresponding column profiles.

        Returns:
            List of semantic_synonym edges with confidence scores.
        """
        edges: list[Edge] = []
        profile_map = {p.column_id: p for p in profiles}

        # Only compare columns from different tables
        for i, col_a in enumerate(columns):
            for col_b in columns[i + 1 :]:
                # Skip same-table comparisons
                if col_a.table_id == col_b.table_id:
                    continue

                profile_a = profile_map.get(col_a.id)
                profile_b = profile_map.get(col_b.id)
                if profile_a is None or profile_b is None:
                    continue

                confidence = self._compute_confidence(col_a, col_b, profile_a, profile_b)
                if confidence > 0.0:
                    edge = Edge(
                        id=f"edge:synonym:{col_a.id}:{col_b.id}",
                        source_id=col_a.id,
                        target_id=col_b.id,
                        type=EdgeType.SEMANTIC_SYNONYM,
                        confidence=confidence,
                        properties={
                            "name_similarity": self._name_similarity(col_a.name, col_b.name),
                            "semantic_type_match": profile_a.semantic_type == profile_b.semantic_type,
                            "semantic_type_a": profile_a.semantic_type,
                            "semantic_type_b": profile_b.semantic_type,
                        },
                    )
                    edges.append(edge)
                    logger.debug(f"Found synonym: {col_a.name} ≡ {col_b.name} (confidence={confidence:.2f})")

        logger.info(f"Found {len(edges)} semantic synonym edges")
        return edges

    def _compute_confidence(
        self,
        col_a: ColumnNode,
        col_b: ColumnNode,
        profile_a: ColumnProfile,
        profile_b: ColumnProfile,
    ) -> float:
        """Compute synonym confidence between two columns."""
        # Check semantic type match
        type_match = False
        if profile_a.semantic_type != "unknown" and profile_b.semantic_type != "unknown":
            if profile_a.semantic_type == profile_b.semantic_type:
                type_match = True

        # Check name similarity
        name_sim = self._name_similarity(col_a.name, col_b.name)

        # Check synonym group membership
        group_match = self._check_synonym_group(col_a.name, col_b.name)

        # Compute final confidence
        if type_match and (name_sim > 0.5 or group_match):
            return 0.95  # Very high confidence
        elif type_match:
            return 0.85  # High confidence
        elif group_match:
            return 0.80  # High confidence
        elif name_sim > 0.7:
            return 0.6  # Medium confidence
        elif name_sim > 0.5:
            return 0.4  # Low confidence

        return 0.0  # Not a synonym

    def _name_similarity(self, name_a: str, name_b: str) -> float:
        """Compute name similarity between two column names.

        Uses normalized Levenshtein-like distance and substring matching.
        """
        # Normalize names: remove underscores, lowercase
        norm_a = name_a.lower().replace("_", "")
        norm_b = name_b.lower().replace("_", "")

        if norm_a == norm_b:
            return 1.0

        # Check substring containment
        if norm_a in norm_b or norm_b in norm_a:
            # Weight by length ratio (shorter name contained in longer)
            ratio = min(len(norm_a), len(norm_b)) / max(len(norm_a), len(norm_b))
            return 0.5 + 0.5 * ratio  # 0.5-1.0 range

        # Compute character-level overlap
        set_a = set(norm_a)
        set_b = set(norm_b)
        if not set_a or not set_b:
            return 0.0

        jaccard = len(set_a & set_b) / len(set_a | set_b)

        # Also check prefix match
        common_prefix_len = 0
        for i in range(min(len(norm_a), len(norm_b))):
            if norm_a[i] == norm_b[i]:
                common_prefix_len += 1
            else:
                break
        prefix_score = common_prefix_len / max(len(norm_a), len(norm_b))

        return max(jaccard, prefix_score)

    def _check_synonym_group(self, name_a: str, name_b: str) -> bool:
        """Check if two column names belong to the same known synonym group."""
        norm_a = name_a.lower().replace("-", "_")
        norm_b = name_b.lower().replace("-", "_")

        for group in self.SYNONYM_GROUPS:
            if norm_a in group and norm_b in group:
                return True
        return False
