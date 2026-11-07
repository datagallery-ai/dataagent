"""Tests for the CLI commands."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from datalink.cli.main import app
from datalink.config import DataLinkConfig, LLMConfig
from datalink.graph.storage import GraphStorage
from datalink.mapper.llm_mapper import LLMMapper

runner = CliRunner()

TEST_DATA_DIR = Path(__file__).parent / "test_data"

# Heuristic column-name → (concept_name, description, unit, dimension)
_COLUMN_CONCEPTS: dict[str, tuple[str, str, str, str]] = {
    "id": ("record_identifier", "Unique record identifier", "", "identifier"),
    "customer_id": ("person_identifier", "Person identifier", "", "identifier"),
    "user_id": ("person_identifier", "Person identifier", "", "identifier"),
    "order_id": ("order_identifier", "Order identifier", "", "identifier"),
    "tx_id": ("transaction_identifier", "Transaction identifier", "", "identifier"),
    "name": ("person_name", "Person name", "", "text"),
    "email": ("email_address", "Email address", "", "contact"),
    "amount": ("monetary_value", "Monetary amount", "USD", "monetary"),
    "value": ("monetary_value", "Monetary amount", "USD", "monetary"),
    "signup_date": ("signup_date", "Account signup date", "", "temporal"),
    "order_date": ("order_date", "Order date", "", "temporal"),
    "tx_timestamp": ("transaction_timestamp", "Transaction timestamp", "", "temporal"),
    "age": ("person_age", "Person age", "years", "demographic"),
    "status": ("order_status", "Order status", "", "categorical"),
    "tx_type": ("transaction_type", "Transaction type", "", "categorical"),
}

_TABLE_ENTITIES: dict[str, tuple[str, str]] = {
    "users": ("user", "A registered user account"),
    "orders": ("order", "A customer order"),
    "transactions": ("transaction", "A financial transaction"),
}


def _make_mock_llm_response(prompt: str) -> str:
    """Build a valid LLM JSON response from column metadata embedded in the prompt."""
    columns: list[dict] = []
    for line in prompt.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "column_id" in data:
            columns.append(data)

    if not columns:
        return json.dumps({"concepts": [], "entities": []})

    # Group columns by concept name (merge synonyms like customer_id + user_id)
    concept_map: dict[str, dict] = {}
    for col in columns:
        col_name = col["column_name"].lower()
        concept_name, desc, unit, dimension = _COLUMN_CONCEPTS.get(
            col_name, (f"{col_name}_attribute", f"Attribute {col_name}", "", "other")
        )
        if concept_name not in concept_map:
            concept_map[concept_name] = {
                "name": concept_name,
                "description": desc,
                "unit": unit,
                "dimension": dimension,
                "columns": [],
                "confidence": 0.85,
            }
        concept_map[concept_name]["columns"].append(col["column_id"])

    # Build entities grouped by table
    table_concepts: dict[str, set[str]] = {}
    for col in columns:
        table_id = col.get("table_id", "")
        table_name = table_id.rsplit(":", 1)[-1] if table_id else "unknown"
        col_name = col["column_name"].lower()
        concept_name = _COLUMN_CONCEPTS.get(col_name, (f"{col_name}_attribute",))[0]
        table_concepts.setdefault(table_name, set()).add(concept_name)

    entities = []
    for table_name, concept_names in table_concepts.items():
        entity_name, entity_desc = _TABLE_ENTITIES.get(table_name, (table_name, f"Entity {table_name}"))
        entities.append(
            {
                "name": entity_name,
                "description": entity_desc,
                "concept_names": sorted(concept_names),
                "confidence": 0.8,
            }
        )

    return json.dumps({"concepts": list(concept_map.values()), "entities": entities})


def _mock_call_llm(_self, prompt: str, temperature: float | None = None) -> str:
    """Mock LLM call that handles mapping, merge, and table comment prompts."""
    # Detect prompt type by content
    if "NEW concepts" in prompt or "EXISTING concepts" in prompt:
        # Merge prompt — return no merges (keep all new nodes)
        return json.dumps({"merges": [], "new_kept": []})
    elif "Table data to analyze" in prompt or "tables_data" in prompt:
        # Table comment prompt — return comments for each table_id found in prompt
        table_ids = []
        for line in prompt.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
                if "table_id" in data:
                    table_ids.append(data["table_id"])
            except json.JSONDecodeError:
                continue
        return json.dumps(
            {
                "tables": [
                    {"table_id": tid, "comment": f"Table containing {tid.split(':')[-1]} data"} for tid in table_ids
                ]
            }
        )
    else:
        # Mapping prompt
        return _make_mock_llm_response(prompt)


def _llm_api_configured() -> bool:
    """Return True when a real LLM API key is available."""
    return bool(DataLinkConfig.load().llm.get_api_key())


def _require_semantic_mapping(stats: dict) -> None:
    """Skip when the pipeline ran but LLM produced no semantic nodes."""
    if stats["node_type_counts"].get("concept", 0) == 0:
        pytest.skip("LLM produced no semantic nodes (check API key, base_url, and model)")


@pytest.fixture
def require_real_llm():
    """Skip the test when no LLM API key is configured."""
    if not _llm_api_configured():
        pytest.skip("No LLM API key configured (set datalink_config.json or OPENAI_API_KEY)")


@pytest.fixture(autouse=True)
def mock_llm_and_config(request, monkeypatch):
    """Avoid real LLM calls and real datalink_config.json during CLI tests."""
    if request.node.get_closest_marker("real_llm"):
        yield
        return

    test_config = DataLinkConfig(
        llm=LLMConfig(model="gpt-4o", api_key="test-key"),
    )
    monkeypatch.setattr("datalink.cli.main.DataLinkConfig.load", lambda path=None: test_config)

    with patch.object(LLMMapper, "_call_llm", _mock_call_llm):
        yield


@pytest.fixture
def temp_db():
    """Create a temporary database path for CLI tests."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # On Windows, SQLite files may still be locked; try multiple times
    for _ in range(5):
        try:
            if os.path.exists(db_path):
                os.unlink(db_path)
            break
        except PermissionError:
            import time

            time.sleep(0.5)


class TestCLIAddTable:
    """Test add-table command."""

    def test_add_table_all_tables_initial_build(self, temp_db):
        """add-table without --table on empty graph = initial build."""
        result = runner.invoke(
            app,
            [
                "add-table",
                "--source",
                str(TEST_DATA_DIR),
                "--db",
                temp_db,
            ],
        )
        assert result.exit_code == 0
        assert "Added" in result.output

    def test_add_single_table(self, temp_db):
        """add-table with --table adds one specific table."""
        # First add all from users.csv (initial build)
        runner.invoke(
            app,
            [
                "add-table",
                "--source",
                str(TEST_DATA_DIR / "users.csv"),
                "--db",
                temp_db,
            ],
        )

        # Then add orders table
        result = runner.invoke(
            app,
            [
                "add-table",
                "--source",
                str(TEST_DATA_DIR / "orders.csv"),
                "--table",
                "orders",
                "--db",
                temp_db,
            ],
        )
        assert result.exit_code == 0
        assert "Added table" in result.output

    def test_add_table_incremental_all(self, temp_db):
        """add-table without --table adds all tables from source incrementally."""
        # Initial build with users
        runner.invoke(
            app,
            [
                "add-table",
                "--source",
                str(TEST_DATA_DIR / "users.csv"),
                "--db",
                temp_db,
            ],
        )

        # Add remaining tables from the directory
        result = runner.invoke(
            app,
            [
                "add-table",
                "--source",
                str(TEST_DATA_DIR),
                "--db",
                temp_db,
            ],
        )
        assert result.exit_code == 0
        assert "Added" in result.output


class TestCLIRebuild:
    """Test the rebuild command."""

    def test_rebuild_after_add_table(self, temp_db):
        """rebuild works after add-table creates a graph."""
        runner.invoke(
            app,
            [
                "add-table",
                "--source",
                str(TEST_DATA_DIR),
                "--db",
                temp_db,
            ],
        )

        result = runner.invoke(app, ["rebuild", "--db", temp_db])
        assert result.exit_code == 0
        assert "Full rebuild complete" in result.output

    def test_rebuild_empty_graph(self, temp_db):
        """rebuild on empty graph should fail."""
        result = runner.invoke(app, ["rebuild", "--db", temp_db])
        assert result.exit_code == 1


class TestCLIRemoveTable:
    """Test remove-table command."""

    def test_remove_table(self, temp_db):
        # Build graph first
        runner.invoke(app, ["add-table", "--source", str(TEST_DATA_DIR), "--db", temp_db])

        # Remove a table by name
        result = runner.invoke(
            app,
            [
                "remove-table",
                "--table",
                "transactions",
                "--db",
                temp_db,
            ],
        )
        assert result.exit_code == 0
        assert "Removed table" in result.output


class TestCLISearch:
    """Test the search command."""

    def test_search_after_build(self, temp_db):
        runner.invoke(app, ["add-table", "--source", str(TEST_DATA_DIR), "--db", temp_db])

        result = runner.invoke(app, ["search", "customer_id", "--db", temp_db])
        assert result.exit_code == 0
        assert "customer_id" in result.output

    def test_search_with_type_filter(self, temp_db):
        runner.invoke(app, ["add-table", "--source", str(TEST_DATA_DIR), "--db", temp_db])

        result = runner.invoke(app, ["search", "id", "--type", "column", "--db", temp_db])
        assert result.exit_code == 0

    def test_search_nonexistent(self, temp_db):
        runner.invoke(app, ["add-table", "--source", str(TEST_DATA_DIR), "--db", temp_db])

        result = runner.invoke(app, ["search", "xyz_nonexistent", "--db", temp_db])
        assert result.exit_code == 0
        assert "No results" in result.output


class TestCLIInfo:
    """Test the info command."""

    def test_info_after_build(self, temp_db):
        runner.invoke(app, ["add-table", "--source", str(TEST_DATA_DIR), "--db", temp_db])

        result = runner.invoke(app, ["info", "--db", temp_db])
        assert result.exit_code == 0
        assert "DataLink Overview" in result.output
        assert "Total Nodes" in result.output


class TestCLIConfig:
    """Test the config command."""

    def test_config_set_llm(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "config",
                "--llm-model",
                "claude-sonnet-4-6",
                "--llm-base-url",
                "https://api.anthropic.com/v1",
            ],
        )
        assert result.exit_code == 0
        assert "Configuration saved" in result.output

        saved = json.loads((tmp_path / "datalink_config.json").read_text())
        assert saved["llm"]["model"] == "claude-sonnet-4-6"
        assert saved["llm"]["base_url"] == "https://api.anthropic.com/v1"

    def test_config_show_values(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["config"])
        assert result.exit_code == 0


@pytest.mark.real_llm
class TestCLIRealLLM:
    """Optional integration tests that call a real LLM (skipped without API key)."""

    def test_add_table_single_small_csv(self, temp_db, require_real_llm):
        """add-table on users.csv (5 columns) succeeds with real semantic mapping."""
        result = runner.invoke(
            app,
            [
                "add-table",
                "--source",
                str(TEST_DATA_DIR / "users.csv"),
                "--db",
                temp_db,
            ],
        )
        assert result.exit_code == 0
        assert "Added" in result.output

        storage = GraphStorage(temp_db)
        try:
            stats = storage.get_graph_stats()
            assert stats["node_type_counts"]["table"] == 1
            assert stats["node_type_counts"]["column"] == 5
            _require_semantic_mapping(stats)
            assert stats["node_type_counts"]["entity"] > 0
        finally:
            storage.close()

    def test_add_two_tables_info_command(self, temp_db, require_real_llm):
        """add-table on users + orders, then info reports semantic nodes."""
        for csv_name in ("users.csv", "orders.csv"):
            result = runner.invoke(
                app,
                [
                    "add-table",
                    "--source",
                    str(TEST_DATA_DIR / csv_name),
                    "--db",
                    temp_db,
                ],
            )
            assert result.exit_code == 0, result.output

        storage = GraphStorage(temp_db)
        try:
            stats = storage.get_graph_stats()
            _require_semantic_mapping(stats)
        finally:
            storage.close()

        result = runner.invoke(app, ["info", "--db", temp_db])
        assert result.exit_code == 0
        assert "DataLink Overview" in result.output
        assert "Concepts" in result.output
        assert "Entities" in result.output
