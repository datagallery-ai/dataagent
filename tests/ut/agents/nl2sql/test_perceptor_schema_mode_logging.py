from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

import dataagent.agents.nl2sql.nodes.perceptor as perceptor_module
from dataagent.agents.nl2sql.nodes.perceptor import PerceptorNode
from dataagent.agents.nl2sql.workflow.state import get_default_state


def _schema() -> dict:
    return {
        "orders": {
            "description": "orders",
            "columns": {
                "order_id": {
                    "description": "identifier",
                    "value_type": "integer",
                    "example_values": "1",
                }
            },
        }
    }


@pytest.mark.parametrize("schema_mode", ["full_schema", "schema_linking"])
@pytest.mark.asyncio
async def test_perceptor_logs_effective_schema_mode(
    schema_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Perceptor should log the schema mode selected for remote schema perception."""
    node = PerceptorNode(schema_mode=schema_mode)
    full_schema = Mock(return_value=(_schema(), []))
    schema_linking = Mock(return_value=(_schema(), []))
    logger = Mock()
    monkeypatch.setattr(node, "full_schema", full_schema)
    monkeypatch.setattr(node, "schema_linking", schema_linking)
    monkeypatch.setattr(perceptor_module, "logger", logger)

    await node._aprocess(get_default_state("list orders"))

    logger.debug.assert_called_once_with("NL2SQL Perceptor using schema_mode={}", schema_mode)
    if schema_mode == "schema_linking":
        schema_linking.assert_called_once_with(["list orders"])
        full_schema.assert_not_called()
    else:
        full_schema.assert_called_once_with()
        schema_linking.assert_not_called()


@pytest.mark.asyncio
async def test_perceptor_does_not_log_schema_mode_when_user_schema_bypasses_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-provided schema should bypass both schema-mode selection and its debug log."""
    schema_path = tmp_path / "schema.md"
    schema_path.write_text("CREATE TABLE orders (order_id INTEGER);", encoding="utf-8")
    node = PerceptorNode(schema_mode="schema_linking", user_schema=str(schema_path))
    logger = Mock()
    monkeypatch.setattr(node, "full_schema", Mock(side_effect=AssertionError("full_schema must not run")))
    monkeypatch.setattr(node, "schema_linking", Mock(side_effect=AssertionError("schema_linking must not run")))
    monkeypatch.setattr(perceptor_module, "logger", logger)

    await node._aprocess(get_default_state("list orders"))

    logger.debug.assert_not_called()
