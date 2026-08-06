"""Tests for the tabular profiler."""

import pandas as pd

from datalink.connector.file import FileConnector
from datalink.models.datasource import DatasourceConfig, DatasourceType
from datalink.profiler.tabular import TabularProfiler


class TestTabularProfiler:
    """Test column profiling."""

    def test_profile_datasource(self, all_csv_paths):
        config = DatasourceConfig(
            type=DatasourceType.CSV,
            name="test",
            path=str(all_csv_paths[0].parent),
        )
        connector = FileConnector(config)
        ds_info = connector.get_datasource_info()

        profiler = TabularProfiler()
        profiles = profiler.profile_datasource(ds_info)

        # 3 tables, total 15 columns
        assert len(profiles) == 15

    def test_profile_integer_column(self):
        series = pd.Series([1, 2, 3, 5, 8, 2, 3, 1, 9, 10])
        profiler = TabularProfiler()
        profile = profiler.profile_column(
            column_id="col:test:t:id",
            column_name="id",
            series=series,
        )
        assert profile.dtype == "integer"
        assert profile.semantic_type == "identifier"  # high unique rate + name "id"
        assert profile.null_rate == 0.0
        assert profile.min_value == 1.0
        assert profile.max_value == 10.0

    def test_profile_float_column(self):
        series = pd.Series([85.5, 120.0, 45.3, 200.0, 78.9])
        profiler = TabularProfiler()
        profile = profiler.profile_column(
            column_id="col:test:t:amount",
            column_name="amount",
            series=series,
        )
        assert profile.dtype == "float"
        assert profile.semantic_type == "monetary_value"  # name matches *_amount
        assert profile.min_value == 45.3

    def test_profile_string_column(self):
        series = pd.Series(["alice@example.com", "bob@example.com", "charlie@example.com"])
        profiler = TabularProfiler()
        profile = profiler.profile_column(
            column_id="col:test:t:email",
            column_name="email",
            series=series,
        )
        assert profile.dtype == "string"
        assert profile.semantic_type == "email_address"  # value pattern + name
        assert "email_pattern" in profile.value_patterns
        assert profile.min_length is not None
        assert profile.max_length is not None

    def test_profile_datetime_column(self):
        series = pd.Series(pd.to_datetime(["2024-01-20", "2024-03-01", "2024-05-01"]))
        profiler = TabularProfiler()
        profile = profiler.profile_column(
            column_id="col:test:t:order_date",
            column_name="order_date",
            series=series,
        )
        assert profile.dtype == "datetime"
        assert profile.semantic_type == "timestamp"  # name matches *_date

    def test_profile_null_column(self):
        series = pd.Series([1, None, 3, None, 5, 6, 7, None, 9, 10])
        profiler = TabularProfiler()
        profile = profiler.profile_column(
            column_id="col:test:t:value",
            column_name="value",
            series=series,
        )
        assert profile.null_rate == 0.3  # 3 nulls out of 10
        assert profile.total_count == 10

    def test_profile_identifier_column(self):
        series = pd.Series(range(1, 101))  # 100 unique integer values
        profiler = TabularProfiler()
        profile = profiler.profile_column(
            column_id="col:test:t:user_id",
            column_name="user_id",
            series=series,
        )
        assert profile.unique_rate == 1.0
        assert profile.semantic_type == "identifier"

    def test_profile_low_cardinality_column(self):
        series = pd.Series(["completed", "pending", "completed", "completed", "cancelled"] * 20)
        profiler = TabularProfiler()
        profile = profiler.profile_column(
            column_id="col:test:t:status",
            column_name="status",
            series=series,
        )
        assert profile.semantic_type == "status_enum"  # name matches *_status
        assert profile.cardinality == 3
        assert len(profile.top_values) > 0
        # Top value should be "completed" (60 occurrences out of 100)
        top = profile.top_values[0]
        assert top["value"] == "completed"

    def test_sample_values_populated(self):
        series = pd.Series(range(100))
        profiler = TabularProfiler()
        profile = profiler.profile_column(
            column_id="col:test:t:id",
            column_name="id",
            series=series,
        )
        assert len(profile.sample_values) == 5

    def test_distribution_histogram(self):
        series = pd.Series(range(100))
        profiler = TabularProfiler()
        profile = profiler.profile_column(
            column_id="col:test:t:value",
            column_name="value",
            series=series,
        )
        assert len(profile.distribution_histogram) > 0
        # Each histogram entry should have bin_start, bin_end, count
        entry = profile.distribution_histogram[0]
        assert "bin_start" in entry
        assert "bin_end" in entry
        assert "count" in entry

    def test_profile_real_users_table(self, users_csv_path):
        config = DatasourceConfig(
            type=DatasourceType.CSV,
            name="test",
            path=str(users_csv_path),
        )
        connector = FileConnector(config)
        ds_info = connector.get_datasource_info()

        profiler = TabularProfiler()
        profiles = profiler.profile_datasource(ds_info)

        # Should have 5 profiles for 5 columns
        assert len(profiles) == 5

        # id column → identifier
        id_profile = [p for p in profiles if "id" in p.column_id][0]
        assert id_profile.dtype == "integer"
        assert id_profile.semantic_type == "identifier"

        # email column → email_address
        email_profile = [p for p in profiles if "email" in p.column_id][0]
        assert email_profile.dtype == "string"
        assert email_profile.semantic_type == "email_address"

        # age column → unknown or integer
        age_profile = [p for p in profiles if "age" in p.column_id][0]
        assert age_profile.dtype == "integer"
