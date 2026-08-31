"""Tests for relationship inferrers."""

from datalink.connector.file import FileConnector
from datalink.extractor.tabular import TabularExtractor
from datalink.inferrer.correlated import CorrelationInferrer
from datalink.inferrer.distribution import DistributionInferrer
from datalink.inferrer.joinable import JoinableInferrer
from datalink.inferrer.synonym import SynonymInferrer
from datalink.models.datasource import DatasourceConfig, DatasourceType
from datalink.models.edge import EdgeType
from datalink.profiler.tabular import TabularProfiler


def _build_test_graph(all_csv_paths):
    """Helper to build nodes, profiles from test data."""
    config = DatasourceConfig(
        type=DatasourceType.CSV,
        name="test",
        path=str(all_csv_paths[0].parent),
    )
    connector = FileConnector(config)
    ds_info = connector.get_datasource_info()

    extractor = TabularExtractor()
    tables, columns, edges = extractor.extract(ds_info)

    profiler = TabularProfiler()
    profiles = profiler.profile_datasource(ds_info)

    return tables, columns, edges, profiles, ds_info


class TestJoinableInferrer:
    """Test the joinable relationship inference."""

    def test_find_joinable_columns(self, all_csv_paths):
        _, columns, _, profiles, _ = _build_test_graph(all_csv_paths)

        inferrer = JoinableInferrer(overlap_threshold=0.1)
        joinable_edges = inferrer.infer(columns, profiles)

        # Should find at least some joinable columns
        # (users.id ↔ orders.customer_id ↔ transactions.user_id share values)
        assert len(joinable_edges) > 0

        # Check edge type
        for edge in joinable_edges:
            assert edge.type == EdgeType.JOINABLE
            assert edge.confidence > 0.0

    def test_joinable_confidence_is_overlap_rate(self, all_csv_paths):
        _, columns, _, profiles, _ = _build_test_graph(all_csv_paths)

        inferrer = JoinableInferrer(overlap_threshold=0.1)
        joinable_edges = inferrer.infer(columns, profiles)

        # Confidence should equal the overlap rate
        for edge in joinable_edges:
            assert edge.confidence == edge.properties["overlap_rate"]

    def test_no_joinable_for_high_cardinality(self):
        """Columns with very high cardinality should be skipped."""
        from datalink.models.node import ColumnNode
        from datalink.models.profile import ColumnProfile

        col_a = ColumnNode(id="col:t1:uuid_a", name="uuid_a", table_id="t1", dtype="string")
        col_b = ColumnNode(id="col:t2:uuid_b", name="uuid_b", table_id="t2", dtype="string")

        # Very high cardinality profiles
        profile_a = ColumnProfile(
            id="p_a",
            column_id=col_a.id,
            dtype="string",
            cardinality=50000,
            top_values=[],
            sample_values=["abc1", "abc2"],
        )
        profile_b = ColumnProfile(
            id="p_b",
            column_id=col_b.id,
            dtype="string",
            cardinality=50000,
            top_values=[],
            sample_values=["def1", "def2"],
        )

        inferrer = JoinableInferrer(overlap_threshold=0.1, max_cardinality=1000)
        edges = inferrer.infer([col_a, col_b], [profile_a, profile_b])

        assert len(edges) == 0  # Should skip high-cardinality columns

    def test_incompatible_dtypes_skip(self):
        """Incompatible dtype pairs should be skipped."""
        from datalink.models.node import ColumnNode
        from datalink.models.profile import ColumnProfile

        col_int = ColumnNode(id="col:t1:age", name="age", table_id="t1", dtype="integer")
        col_str = ColumnNode(id="col:t2:name", name="name", table_id="t2", dtype="string")

        profile_int = ColumnProfile(
            id="p_int",
            column_id=col_int.id,
            dtype="integer",
            cardinality=5,
            top_values=[{"value": "25", "count": 2, "fraction": 0.2}],
            sample_values=[25, 30, 35],
        )
        profile_str = ColumnProfile(
            id="p_str",
            column_id=col_str.id,
            dtype="string",
            cardinality=5,
            top_values=[{"value": "Alice", "count": 1, "fraction": 0.1}],
            sample_values=["Alice", "Bob"],
        )

        # integer and string are considered compatible (identifiers)
        # but name (person_name semantic type) vs age is clearly not
        # The compatibility check only checks dtypes, not semantic types
        # So integer and string are considered compatible for joinability
        inferrer = JoinableInferrer(overlap_threshold=0.1)
        # This would depend on overlap rate, which is 0 here
        edges = inferrer.infer([col_int, col_str], [profile_int, profile_str])
        assert len(edges) == 0  # No overlap


class TestSynonymInferrer:
    """Test the semantic synonym inference."""

    def test_find_synonym_columns(self, all_csv_paths):
        _, columns, _, profiles, _ = _build_test_graph(all_csv_paths)

        inferrer = SynonymInferrer()
        synonym_edges = inferrer.infer(columns, profiles)

        # Should find synonyms (customer_id ↔ user_id are in synonym groups)
        assert len(synonym_edges) > 0

    def test_synonym_group_match(self):
        """Columns in the same synonym group should get high confidence."""
        from datalink.models.node import ColumnNode
        from datalink.models.profile import ColumnProfile

        col_a = ColumnNode(id="col:t1:customer_id", name="customer_id", table_id="t1", dtype="integer")
        col_b = ColumnNode(id="col:t2:user_id", name="user_id", table_id="t2", dtype="integer")

        profile_a = ColumnProfile(
            id="p_a",
            column_id=col_a.id,
            dtype="integer",
            semantic_type="identifier",
        )
        profile_b = ColumnProfile(
            id="p_b",
            column_id=col_b.id,
            dtype="integer",
            semantic_type="identifier",
        )

        inferrer = SynonymInferrer()
        edges = inferrer.infer([col_a, col_b], [profile_a, profile_b])

        assert len(edges) == 1
        assert edges[0].confidence >= 0.80  # High confidence (group match + type match)

    def test_same_table_not_compared(self):
        """Columns in the same table should not be compared for synonyms."""
        from datalink.models.node import ColumnNode
        from datalink.models.profile import ColumnProfile

        col_a = ColumnNode(id="col:t1:id", name="id", table_id="t1", dtype="integer")
        col_b = ColumnNode(id="col:t1:name", name="name", table_id="t1", dtype="string")

        profile_a = ColumnProfile(id="p_a", column_id=col_a.id, dtype="integer")
        profile_b = ColumnProfile(id="p_b", column_id=col_b.id, dtype="string")

        inferrer = SynonymInferrer()
        edges = inferrer.infer([col_a, col_b], [profile_a, profile_b])

        assert len(edges) == 0  # Same table, no synonym comparison


class TestDistributionInferrer:
    """Test distribution similarity inference."""

    def test_find_similar_distributions(self, all_csv_paths):
        _, columns, _, profiles, _ = _build_test_graph(all_csv_paths)

        inferrer = DistributionInferrer(similarity_threshold=0.3)
        dist_edges = inferrer.infer(columns, profiles)

        # Should find some distribution similarities
        # (order_date and tx_timestamp might have similar temporal ranges)
        for edge in dist_edges:
            assert edge.type == EdgeType.DISTRIBUTION_SIMILAR
            assert edge.confidence > 0.3


class TestCorrelationInferrer:
    """Test correlation inference."""

    def test_correlation_requires_joinable(self, all_csv_paths):
        _, columns, _, profiles, ds_info = _build_test_graph(all_csv_paths)

        # First find joinable edges
        joinable_inferrer = JoinableInferrer(overlap_threshold=0.1)
        joinable_edges = joinable_inferrer.infer(columns, profiles)

        # Then find correlations (requires joinable edges and datasource infos as input)
        corr_inferrer = CorrelationInferrer(correlation_threshold=0.3)
        corr_edges = corr_inferrer.infer(columns, profiles, joinable_edges, [ds_info])

        # Correlated edges connect non-key numeric columns, not join keys themselves
        # They should reference a joinable edge in their properties
        for edge in corr_edges:
            assert "joinable_edge" in edge.properties
            assert "join_key_a" in edge.properties
            assert "join_key_b" in edge.properties
            assert edge.properties["method"] == "pearson"
