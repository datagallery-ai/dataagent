"""Tests for credential masking utilities."""

from datalink.utils.credential import (
    build_id_mapping,
    is_masked_id,
    mask_credentials,
    mask_id,
    mask_result,
    resolve_masked_id,
    resolve_masked_ids,
)


class TestMaskCredentials:
    """Test mask_credentials function."""

    def test_postgresql_with_user_and_password(self):
        url = "postgresql://admin:secret123@db.example.com:5432/mydb"
        assert mask_credentials(url) == "postgresql://***:***@db.example.com:5432/mydb"

    def test_mysql_with_user_and_password(self):
        url = "mysql://root:pass@localhost:3306/sales"
        assert mask_credentials(url) == "mysql://***:***@localhost:3306/sales"

    def test_url_with_user_no_password(self):
        url = "postgresql://user@host/db"
        assert mask_credentials(url) == "postgresql://***@host/db"

    def test_url_with_password_only(self):
        # URL with user:pass but user is empty — still masks both
        url = "postgresql://:pass@host/db"
        result = mask_credentials(url)
        assert "pass" not in result
        assert "***" in result

    def test_sqlite_no_credentials(self):
        url = "sqlite:///path/to/db.sqlite"
        assert mask_credentials(url) == "sqlite:///path/to/db.sqlite"

    def test_sqlite_memory(self):
        url = "sqlite://"
        assert mask_credentials(url) == "sqlite://"

    def test_file_path_not_masked(self):
        path = "/home/user/data/orders.csv"
        assert mask_credentials(path) == "/home/user/data/orders.csv"

    def test_windows_path_not_masked(self):
        path = "C:\\Users\\admin\\data\\orders.csv"
        assert mask_credentials(path) == "C:\\Users\\admin\\data\\orders.csv"

    def test_empty_string(self):
        assert mask_credentials("") == ""

    def test_simple_name_not_masked(self):
        assert mask_credentials("unknown") == "unknown"

    def test_url_with_query_params(self):
        url = "postgresql://admin:pass@host/db?sslmode=require"
        result = mask_credentials(url)
        assert "admin" not in result
        assert "pass" not in result
        assert "sslmode=require" in result

    def test_url_without_port(self):
        url = "postgresql://admin:pass@host/mydb"
        assert mask_credentials(url) == "postgresql://***:***@host/mydb"


class TestMaskId:
    """Test mask_id function for node IDs."""

    def test_table_id_with_db_connection(self):
        id = "table:postgresql://admin:secret@host/db:orders"
        assert mask_id(id) == "table:postgresql://***:***@host/db:orders"

    def test_column_id_with_db_connection(self):
        id = "column:postgresql://admin:secret@host/db:orders:customer_id"
        assert mask_id(id) == "column:postgresql://***:***@host/db:orders:customer_id"

    def test_table_id_with_file_path(self):
        id = "table:/home/user/data:orders"
        assert mask_id(id) == "table:/home/user/data:orders"

    def test_column_id_with_file_path(self):
        id = "column:/home/user/data:orders:amount"
        assert mask_id(id) == "column:/home/user/data:orders:amount"

    def test_empty_id(self):
        assert mask_id("") == ""

    def test_simple_id_no_colons(self):
        assert mask_id("concept") == "concept"

    def test_profile_id_with_db_connection(self):
        id = "profile:column:postgresql://admin:pass@host/db:orders:col"
        result = mask_id(id)
        assert "admin" not in result
        assert "pass" not in result
        assert "***" in result

    def test_edge_id_with_db_connection(self):
        id = "edge:contains:table:postgresql://admin:pass@host/db:orders:column:postgresql://admin:pass@host/db:orders:col"
        result = mask_id(id)
        assert "admin" not in result
        assert "pass" not in result


class TestMaskResult:
    """Test mask_result for full result dicts."""

    def test_mask_search_nodes_result(self):
        result = [
            {
                "id": "column:postgresql://admin:pass@host/db:orders:customer_id",
                "type": "column",
                "name": "customer_id",
                "properties": {
                    "source": "postgresql://admin:pass@host/db",
                    "dtype": "integer",
                },
                "edge_count": 5,
                "edges_summary": [
                    {
                        "type": "joinable",
                        "target_id": "column:postgresql://admin:pass@host/db:users:id",
                        "confidence": 0.8,
                    }
                ],
            }
        ]
        masked = mask_result(result)
        assert "admin" not in masked[0]["id"]
        assert "pass" not in masked[0]["id"]
        assert "***" in masked[0]["id"]
        assert "admin" not in masked[0]["properties"]["source"]
        assert "pass" not in masked[0]["properties"]["source"]
        assert "admin" not in masked[0]["edges_summary"][0]["target_id"]
        assert masked[0]["name"] == "customer_id"  # names not masked
        assert masked[0]["edge_count"] == 5  # ints not masked

    def test_mask_get_node_result(self):
        result = {
            "id": "table:postgresql://admin:pass@host/db:orders",
            "type": "table",
            "name": "orders",
            "properties": {"source": "postgresql://admin:pass@host/db"},
            "edges": [
                {
                    "id": "edge:contains:...",
                    "source_id": "table:postgresql://admin:pass@host/db:orders",
                    "target_id": "column:postgresql://admin:pass@host/db:orders:col",
                    "other_node": {
                        "id": "column:postgresql://admin:pass@host/db:orders:col",
                        "name": "col",
                        "type": "column",
                    },
                }
            ],
        }
        masked = mask_result(result)
        assert "admin" not in masked["id"]
        assert "admin" not in masked["edges"][0]["source_id"]
        assert "admin" not in masked["edges"][0]["target_id"]
        assert "admin" not in masked["edges"][0]["other_node"]["id"]
        assert "admin" not in masked["properties"]["source"]

    def test_mask_find_paths_result(self):
        result = [
            {
                "nodes": [
                    "table:postgresql://admin:pass@host/db:orders",
                    "column:postgresql://admin:pass@host/db:orders:col",
                ],
                "edges": [
                    {
                        "source_id": "table:postgresql://admin:pass@host/db:orders",
                        "target_id": "column:postgresql://admin:pass@host/db:orders:col",
                    }
                ],
            }
        ]
        masked = mask_result(result)
        assert "admin" not in masked[0]["nodes"][0]
        assert "admin" not in masked[0]["edges"][0]["source_id"]

    def test_mask_list_datasets_result(self):
        result = [
            {
                "id": "table:postgresql://admin:pass@host/db:orders",
                "name": "orders",
                "source": "postgresql://admin:pass@host/db",
                "column_count": 5,
            }
        ]
        masked = mask_result(result)
        assert "admin" not in masked[0]["id"]
        assert "admin" not in masked[0]["source"]
        assert "***" in masked[0]["source"]

    def test_mask_file_path_result_not_masked(self):
        result = [
            {
                "id": "table:/home/user/data:orders",
                "name": "orders",
                "source": "/home/user/data",
            }
        ]
        masked = mask_result(result)
        # File paths are not masked — they don't have credentials
        assert masked[0]["id"] == "table:/home/user/data:orders"
        assert masked[0]["source"] == "/home/user/data"

    def test_mask_subgraph_result_nested_ids(self):
        """P0 regression: mask_result must mask table_id, profile_id,
        column_ids inside node properties — these embed DB URLs with
        credentials that leaked in extract_subgraph(mask_credential=True).
        """
        result = {
            "nodes": [
                {
                    "id": "column:postgresql://admin:s3cret@host/db:orders:customer_id",
                    "type": "column",
                    "name": "customer_id",
                    "properties": {
                        "table_id": "table:postgresql://admin:s3cret@host/db:orders",
                        "profile_id": "profile:column:postgresql://admin:s3cret@host/db:orders:customer_id",
                        "column_ids": [
                            "column:postgresql://admin:s3cret@host/db:orders:id",
                            "column:postgresql://admin:s3cret@host/db:orders:amount",
                        ],
                        "source": "postgresql://admin:s3cret@host/db",
                        "dtype": "integer",
                    },
                },
                {
                    "id": "table:postgresql://admin:s3cret@host/db:orders",
                    "type": "table",
                    "name": "orders",
                    "properties": {
                        "table_id": "table:postgresql://admin:s3cret@host/db:orders",
                        "source": "postgresql://admin:s3cret@host/db",
                        "row_count": 500,
                    },
                },
            ],
            "edges": [
                {
                    "id": "edge:contains:table:postgresql://admin:s3cret@host/db:orders:column:postgresql://admin:s3cret@host/db:orders:customer_id",
                    "source_id": "table:postgresql://admin:s3cret@host/db:orders",
                    "target_id": "column:postgresql://admin:s3cret@host/db:orders:customer_id",
                    "type": "contains",
                }
            ],
            "stats": {"node_count": 2, "edge_count": 1},
        }

        masked = mask_result(result)

        # Node IDs must be masked
        assert "s3cret" not in masked["nodes"][0]["id"]
        assert "s3cret" not in masked["nodes"][1]["id"]

        # properties.table_id must be masked (P0 leak point)
        assert "s3cret" not in masked["nodes"][0]["properties"]["table_id"]
        assert "s3cret" not in masked["nodes"][1]["properties"]["table_id"]

        # properties.profile_id must be masked (P0 leak point)
        assert "s3cret" not in masked["nodes"][0]["properties"]["profile_id"]

        # properties.column_ids must be masked element-wise (P0 leak point)
        for col_id in masked["nodes"][0]["properties"]["column_ids"]:
            assert "s3cret" not in col_id

        # properties.source must be masked
        assert "s3cret" not in masked["nodes"][0]["properties"]["source"]

        # Edge IDs and source/target must be masked
        assert "s3cret" not in masked["edges"][0]["id"]
        assert "s3cret" not in masked["edges"][0]["source_id"]
        assert "s3cret" not in masked["edges"][0]["target_id"]

        # Non-credential fields must be preserved
        assert masked["nodes"][0]["name"] == "customer_id"
        assert masked["nodes"][0]["properties"]["dtype"] == "integer"
        assert masked["stats"]["node_count"] == 2

    def test_mask_arbitrary_string_with_embedded_url(self):
        """Catch-all: any string value containing a DB URL must be masked,
        even if the key is not in the known ID/CREDENTIAL key sets.
        """
        result = {
            "nodes": [
                {
                    "id": "table:postgresql://admin:s3cret@host/db:orders",
                    "type": "table",
                    "name": "orders",
                    "properties": {
                        "some_random_key": "postgresql://admin:s3cret@host/db",
                        "another_field": "just a normal string",
                    },
                }
            ]
        }

        masked = mask_result(result)
        # Catch-all masking of strings containing DB URLs
        assert "s3cret" not in masked["nodes"][0]["properties"]["some_random_key"]
        assert "***" in masked["nodes"][0]["properties"]["some_random_key"]
        # Normal strings are not modified
        assert masked["nodes"][0]["properties"]["another_field"] == "just a normal string"

    def test_mask_nested_empty(self):
        assert mask_result([]) == []
        assert mask_result({}) == {}
        assert mask_result(None) is None


class TestIsMaskedId:
    """Test is_masked_id detection."""

    def test_masked_id_detected(self):
        assert is_masked_id("table:postgresql://***:***@host/db:orders")

    def test_masked_id_with_just_user(self):
        assert is_masked_id("table:postgresql://***@host/db:orders")

    def test_unmasked_id_not_detected(self):
        assert not is_masked_id("table:/home/user/data:orders")

    def test_empty_string(self):
        assert not is_masked_id("")

    def test_real_credential_not_detected_as_masked(self):
        # A real credential string contains :pass@, not :***@
        assert not is_masked_id("table:postgresql://admin:pass@host/db:orders")


class TestBuildIdMapping:
    """Test build_id_mapping."""

    def test_mapping_for_db_ids(self):
        ids = {
            "table:postgresql://admin:pass@host/db:orders",
            "column:postgresql://admin:pass@host/db:orders:col",
        }
        mapping = build_id_mapping(ids)
        assert "table:postgresql://***:***@host/db:orders" in mapping
        assert mapping["table:postgresql://***:***@host/db:orders"] == "table:postgresql://admin:pass@host/db:orders"

    def test_mapping_skips_non_maskable_ids(self):
        ids = {"table:/home/data:orders"}
        mapping = build_id_mapping(ids)
        assert len(mapping) == 0  # no mapping needed for file paths

    def test_mapping_is_deterministic(self):
        ids = {"table:postgresql://admin:pass@host/db:orders"}
        m1 = build_id_mapping(ids)
        m2 = build_id_mapping(ids)
        assert m1 == m2


class TestResolveMaskedId:
    """Test resolve_masked_id."""

    def test_resolve_masked_to_real(self):
        mapping = {"table:postgresql://***:***@host/db:orders": "table:postgresql://admin:pass@host/db:orders"}
        masked = "table:postgresql://***:***@host/db:orders"
        assert resolve_masked_id(masked, mapping) == "table:postgresql://admin:pass@host/db:orders"

    def test_unmasked_id_returns_unchanged(self):
        mapping = {"table:postgresql://***:***@host/db:orders": "table:postgresql://admin:pass@host/db:orders"}
        real = "table:/home/data:orders"
        assert resolve_masked_id(real, mapping) == "table:/home/data:orders"

    def test_unknown_masked_id_returns_as_is(self):
        mapping = {}
        masked = "table:postgresql://***:***@host/db:unknown"
        # Not in mapping — return as-is (will likely fail downstream)
        assert resolve_masked_id(masked, mapping) == masked

    def test_resolve_list(self):
        mapping = {
            "table:postgresql://***:***@host/db:orders": "table:postgresql://admin:pass@host/db:orders",
            "column:postgresql://***:***@host/db:orders:col": "column:postgresql://admin:pass@host/db:orders:col",
        }
        result = resolve_masked_ids(
            ["table:postgresql://***:***@host/db:orders", "table:/home/data:users"],
            mapping,
        )
        assert result[0] == "table:postgresql://admin:pass@host/db:orders"
        assert result[1] == "table:/home/data:users"
