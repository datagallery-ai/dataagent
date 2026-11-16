# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Regression tests: raw runtime fragments must never reach public error messages.

Every leak point (model raw output, SQL text, table / column / CTE names) moves
into the server-side ``detail`` field, while the public ``message`` (and the
SEC ``errors[]`` entries) stays fixed text.
"""

from unittest.mock import AsyncMock

import pytest

from dataagent.agents.nl2sql.errors import LLMOutputParseError, SQLSecurityValidationError
from dataagent.agents.nl2sql.nodes.reflector import ReflectorNode
from dataagent.agents.nl2sql.nodes.validator import ValidatorNode
from dataagent.agents.nl2sql.security import check_sql
from dataagent.agents.nl2sql.security import checker as security_checker
from dataagent.agents.nl2sql.security import rules as security_rules
from dataagent.agents.nl2sql.utils.nl2sql_utils import json_parser, metadata_parser, sql_parser
from dataagent.agents.nl2sql.workflow.state import Result
from dataagent.config.config_manager import ConfigManager
from dataagent.interface.rest_api.service import DataAgentService

LEAK = "secleak7301"


def _assert_leaky(text: str | None) -> None:
    """Assert the marker survives, proving the test really exercises the raw fragment."""
    assert text is not None
    assert LEAK in text.lower()


def _assert_clean(text: str) -> None:
    """Assert the marker is absent, proving no raw fragment leaked."""
    assert LEAK not in text.lower()


def _config_manager() -> ConfigManager:
    manager = ConfigManager()
    manager.settings = {"DATABASE": {"dialect": "postgres", "engine": "sqlite", "config": {}}}
    return manager


# --- Leak points 1-4: parser raises in nl2sql_utils -------------------------


@pytest.mark.parametrize(
    ("parser", "raw"),
    [
        (sql_parser, f"model chatter with {LEAK} and no sql block"),
        (json_parser, f"model chatter with {LEAK} and no json block"),
        (metadata_parser, f"model chatter with {LEAK} and no res block"),
    ],
    ids=["no-sql-block", "no-json-block", "no-metadata-block"],
)
def test_parser_missing_block_keeps_raw_output_out_of_public_message(parser, raw: str) -> None:
    """A missing model-output block must produce a fixed message with raw output in detail."""
    with pytest.raises(LLMOutputParseError) as exc_info:
        parser(raw)

    _assert_clean(exc_info.value.message)
    _assert_leaky(exc_info.value.detail)


def test_metadata_parser_invalid_line_keeps_raw_output_out_of_public_message() -> None:
    """A malformed metadata line must produce a fixed message with raw output in detail."""
    text = f"<res>\n{LEAK} => value\n</res>"

    with pytest.raises(LLMOutputParseError) as exc_info:
        metadata_parser(text)

    assert exc_info.value.message == "Metadata block parsing failed"
    _assert_leaky(exc_info.value.detail)


# --- Leak point 5: execute_with_llm_json retry exhaustion --------------------


@pytest.mark.asyncio
async def test_llm_json_missing_block_keeps_raw_output_out_of_public_message() -> None:
    """The retry-exhausted parse error must keep a fixed public message."""
    node = ReflectorNode()
    node.execute_with_llm = AsyncMock(return_value=f"model chatter with {LEAK} and no json block")

    with pytest.raises(LLMOutputParseError) as exc_info:
        await node.execute_with_llm_json({"k": "v"})

    assert exc_info.value.message == "Model output format error: JSON parsing failed after 3 attempts"
    assert exc_info.value.code == "NL2SQL-LLM-002"
    _assert_clean(exc_info.value.message)
    _assert_leaky(exc_info.value.detail)


@pytest.mark.asyncio
async def test_llm_json_malformed_block_captures_block_in_detail() -> None:
    """A present-but-invalid JSON block must be captured verbatim in the detail."""
    node = ReflectorNode()
    node.execute_with_llm = AsyncMock(return_value=f'```json\n{{"sql": {LEAK}}}\n```')

    with pytest.raises(LLMOutputParseError) as exc_info:
        await node.execute_with_llm_json({"k": "v"})

    assert exc_info.value.message == "Model output format error: JSON parsing failed after 3 attempts"
    assert exc_info.value.detail == f'{{"sql": {LEAK}}}'


# --- Leak points 6-7: checker parse / normalization failures -----------------


def test_sql_parse_failure_keeps_parser_error_out_of_public_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parser crash must surface a fixed message with the parser error in detail."""

    def _raise(sql, **kwargs):
        raise RuntimeError(f"unexpected token near {LEAK} in {sql[:32]}")

    monkeypatch.setattr(security_checker.sqlglot, "parse", _raise)
    result = check_sql(
        "SELECT id FROM orders WHERE id = 1", dialect="postgres", schema={"orders": {"columns": {"id": {}}}}
    )

    assert result.blocked is True
    violation = result.violations[0]
    assert violation.rule_id == "SQL-001"
    assert violation.message == "SQL parse failed."
    _assert_leaky(violation.detail)


def test_sql_normalization_failure_keeps_error_text_out_of_public_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed re-parse of normalized SQL must surface a fixed message with the error in detail."""

    def _fake_normalize(*args, **kwargs):
        return f"SELECT {LEAK} !!!"

    monkeypatch.setattr(security_checker, "normalize_semantic_column_references", _fake_normalize)
    result = check_sql(
        "SELECT id FROM orders WHERE id = 1", dialect="postgres", schema={"orders": {"columns": {"id": {}}}}
    )

    assert result.blocked is True
    violation = result.violations[0]
    assert violation.rule_id == "SQL-001"
    assert violation.message == "SQL normalization failed."
    _assert_leaky(violation.detail)


# --- Leak points 8-10: rules with dynamic identifiers -----------------------


def test_grouped_projection_message_hides_column_names() -> None:
    """The GROUP BY violation must not name the offending columns in its message."""
    schema = {"fact_metric": {"columns": {"time": {}, f"{LEAK}_users": {}}}}
    result = check_sql(
        f"SELECT time, {LEAK}_users FROM fact_metric WHERE time > 0 GROUP BY time",
        dialect="postgres",
        schema=schema,
    )

    assert result.blocked is True
    violation = result.violations[0]
    assert violation.rule_id == "SYNTAX-001"
    assert violation.message == (
        "A selected column is not in GROUP BY and is not aggregated, so the query cannot "
        "execute. Wrap a metric in SUM(), or add a dimension to GROUP BY."
    )
    _assert_leaky(violation.detail)


def test_schema_001_message_hides_source_table() -> None:
    """The unresolvable-source violation must keep the table name out of its message."""
    result = check_sql(f"SELECT id FROM pg_catalog.{LEAK}_table WHERE id = 1", dialect="postgres", schema={})

    assert result.blocked is True
    violation = result.violations[0]
    assert violation.rule_id == "SCHEMA-001"
    assert violation.message == "Source table cannot be resolved uniquely in the provided semantic schema."
    _assert_leaky(violation.detail)


def test_schema_002_message_hides_column_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retained column-validation path must keep identifiers out of its message."""
    monkeypatch.setattr(security_rules, "_SEMANTIC_COLUMN_VALIDATION_ENABLED", True)
    schema = {"orders": {"columns": {"id": {}}}}

    result = check_sql(f"SELECT {LEAK}_column FROM orders WHERE id = 1", dialect="postgres", schema=schema)

    assert result.blocked is True
    violation = result.violations[0]
    assert violation.rule_id == "SCHEMA-002"
    assert violation.message == (
        "Column is not exposed by a source available in this query scope. Verify that the column exists in the "
        "referenced physical table's semantic schema or is included in the referenced CTE/subquery SELECT list. "
        "Then use the actual exposed column name with the correct table or CTE alias."
    )
    _assert_leaky(violation.detail)


def test_schema_004_message_hides_column_and_source_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ambiguous-column violation must keep column and source names out of its message."""
    monkeypatch.setattr(security_rules, "_SEMANTIC_COLUMN_VALIDATION_ENABLED", True)
    schema = {
        "orders": {"columns": {"id": {}, LEAK: {}}},
        "events": {"columns": {"id": {}, LEAK: {}}},
    }

    result = check_sql(
        f"SELECT {LEAK} FROM orders CROSS JOIN events WHERE orders.id = events.id", dialect="postgres", schema=schema
    )

    assert result.blocked is True
    violation = result.violations[0]
    assert violation.rule_id == "SCHEMA-004"
    assert violation.message == (
        "Column reference is ambiguous in the current query scope because multiple sources expose it. "
        "This is not a missing semantic-schema column. Qualify every unqualified reference with the "
        "intended source alias."
    )
    _assert_leaky(violation.detail)


# --- REST boundary: message / errors[] must stay clean ----------------------


def test_rest_payload_drops_llm_parse_detail() -> None:
    """The REST layer must drop the raw detail and keep only the fixed message."""
    service = DataAgentService()
    error = LLMOutputParseError(
        "Model output format error: JSON parsing failed after 3 attempts",
        detail=f"no json block found in model output, raw content: {LEAK}",
    )

    payload = service._normalize_error_payload(error.to_dict())

    _assert_clean(str(payload))
    assert payload["result"]["message"] == "Model output format error: JSON parsing failed after 3 attempts"
    assert "detail" not in payload["result"]


def test_rest_security_errors_hide_violation_detail() -> None:
    """Public SEC errors[] must carry only fixed messages, never the violation detail."""
    service = DataAgentService()
    result = check_sql(f"SELECT id FROM {LEAK}_table WHERE id = 1", dialect="postgres", schema={})
    assert result.blocked is True
    _assert_leaky(result.violations[0].detail)

    error = SQLSecurityValidationError(violations=[violation.to_dict() for violation in result.violations])

    payload = service._normalize_error_payload(error.to_dict())
    _assert_clean(str(payload))
    assert payload["result"]["errors"] == [
        {
            "code": "NL2SQL-SEC-013",
            "message": "Source table cannot be resolved uniquely in the provided semantic schema.",
        }
    ]


# --- Detail truncation -------------------------------------------------------


def test_error_detail_is_truncated() -> None:
    """Raw model output entering detail must be capped to keep errors and logs bounded."""
    error = LLMOutputParseError("Model output contains no SQL block", detail="x" * 5000)

    assert error.detail is not None
    assert len(error.detail) == 2003
    assert error.detail.endswith("...")


@pytest.mark.asyncio
async def test_validator_issue_truncates_violation_detail() -> None:
    """Reflector repair issues must stay bounded even for huge SQL fragments."""
    node = ValidatorNode(config_manager=_config_manager(), db_explain=False)
    candidates = [Result(id=0, sql=f"SELECT id FROM {'x' * 5000}{LEAK} WHERE id = 1")]

    result = await node._validate_syntax(candidates, {})

    issue = result[0]["issues"][0]
    assert issue.startswith("SCHEMA-001: Source table cannot be resolved uniquely in the provided semantic schema.")
    assert len(issue) < 2200
