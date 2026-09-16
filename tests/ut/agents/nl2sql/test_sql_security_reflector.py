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
import json
from unittest.mock import AsyncMock

import pytest

from dataagent.agents.nl2sql.errors import SQLSecurityValidationError
from dataagent.agents.nl2sql.nodes.reflector import ReflectorNode
from dataagent.agents.nl2sql.workflow.state import Result, get_default_state


def _candidate(
    candidate_id: int,
    sql: str,
    score: float,
    *,
    blocked: bool = False,
    rule_id: str = "FUNCTION-001",
    violation_message: str = "SQL function is not in the allowlist: current_setting.",
) -> Result:
    violations = [{"rule_id": rule_id, "message": violation_message}] if blocked else []
    return Result(id=candidate_id, sql=sql, score=score, security_checked=True, security_violations=violations)


def test_sql_security_error_maps_every_internal_rule_to_a_public_code() -> None:
    """Every current internal security rule should have a stable public code."""
    expected_codes = {
        "SQL-001": "NL2SQL-SEC-002",
        "SQL-002": "NL2SQL-SEC-003",
        "FUNCTION-001": "NL2SQL-SEC-004",
        "SYNTAX-001": "NL2SQL-SEC-005",
        "RESOURCE-001": "NL2SQL-SEC-006",
        "RESOURCE-002": "NL2SQL-SEC-007",
        "RESOURCE-003": "NL2SQL-SEC-008",
        "RESOURCE-006": "NL2SQL-SEC-009",
        "RESOURCE-007": "NL2SQL-SEC-010",
        "RESOURCE-008": "NL2SQL-SEC-011",
        "RESOURCE-009": "NL2SQL-SEC-012",
        "SCHEMA-001": "NL2SQL-SEC-013",
        "SCHEMA-002": "NL2SQL-SEC-014",
        "SCHEMA-003": "NL2SQL-SEC-015",
        "SCHEMA-004": "NL2SQL-SEC-016",
    }

    for rule_id, expected_code in expected_codes.items():
        error = SQLSecurityValidationError(violations=[{"rule_id": rule_id, "message": "public reason"}])

        assert error.to_dict().get("code") == expected_code
        assert rule_id not in str(error.to_dict())


@pytest.mark.asyncio
async def test_reflector_selects_safe_candidate_before_scoring() -> None:
    """Reflector should never select a blocked candidate with a higher score."""
    node = ReflectorNode(threshold=0.5)
    state = get_default_state("question")
    state["validation_results"] = [
        _candidate(0, "SELECT current_setting('x')", 1.0, blocked=True),
        _candidate(1, "SELECT id FROM orders WHERE id = 1", 0.8),
    ]

    result = await node._aprocess(state)

    assert result["proceed"] is True
    assert result["security_sql_approved"] is True
    assert result["sql"] == "SELECT id FROM orders WHERE id = 1"
    assert [candidate.sql for candidate in result["validation_results"]] == ["SELECT id FROM orders WHERE id = 1"]


@pytest.mark.asyncio
async def test_reflector_raises_security_error_when_all_candidates_blocked_after_retries() -> None:
    """Reflector should fail closed when no safe candidate remains after retries."""
    node = ReflectorNode(threshold=0.9)
    state = get_default_state("question", ref_retries=0)
    state["validation_results"] = [_candidate(0, "SELECT current_setting('x')", 0.0, blocked=True)]

    with pytest.raises(SQLSecurityValidationError) as error:
        await node._aprocess(state)

    payload = error.value.to_dict()
    assert payload.get("code") == "NL2SQL-SEC-004"
    assert payload.get("message") == "SQL function is not in the allowlist: current_setting."
    assert payload.get("errors") == [
        {
            "code": "NL2SQL-SEC-004",
            "message": "SQL function is not in the allowlist: current_setting.",
        }
    ]
    assert "FUNCTION-001" not in str(payload)


@pytest.mark.asyncio
async def test_reflector_returns_multiple_public_security_errors_without_internal_rule_ids() -> None:
    """Multiple violations should retain public details without exposing internal rule identifiers."""
    node = ReflectorNode(threshold=0.9)
    state = get_default_state("question", ref_retries=0)
    state["validation_results"] = [
        _candidate(
            0,
            "SELECT missing_column FROM orders",
            0.0,
            blocked=True,
            rule_id="SCHEMA-002",
            violation_message="Source column is not allowed: missing_column.",
        ),
        _candidate(
            1,
            "SELECT * FROM orders",
            0.0,
            blocked=True,
            rule_id="RESOURCE-009",
            violation_message="Unfiltered row query requires WHERE, HAVING, LIMIT, or FETCH.",
        ),
    ]

    with pytest.raises(SQLSecurityValidationError) as error:
        await node._aprocess(state)

    payload = error.value.to_dict()
    assert payload.get("code") == "NL2SQL-SEC-001"
    assert payload.get("message") == "生成的 SQL 未通过安全校验"
    assert payload.get("errors") == [
        {
            "code": "NL2SQL-SEC-014",
            "message": "Source column is not allowed: missing_column.",
        },
        {
            "code": "NL2SQL-SEC-012",
            "message": "Unfiltered row query requires WHERE, HAVING, LIMIT, or FETCH.",
        },
    ]
    assert "SCHEMA-002" not in str(payload)
    assert "RESOURCE-009" not in str(payload)


@pytest.mark.asyncio
async def test_reflector_rejects_candidate_without_security_proof() -> None:
    """A resume or custom start must not bypass Validator by supplying an unchecked candidate."""
    node = ReflectorNode(threshold=0.0)
    state = get_default_state("question", ref_retries=0)
    state["validation_results"] = [Result(id=0, sql="SELECT id FROM orders", score=1.0)]

    with pytest.raises(SQLSecurityValidationError):
        await node._aprocess(state)


@pytest.mark.asyncio
async def test_reflector_explicit_false_accepts_legacy_validated_candidate() -> None:
    """A disabled security module should allow a candidate checked by the legacy Validator path."""
    node = ReflectorNode(threshold=0.0, sql_security_enabled=False)
    state = get_default_state("question", ref_retries=0)
    state["validation_results"] = [Result(id=0, sql="SELECT id FROM orders", score=1.0)]

    result = await node._aprocess(state)

    assert result.get("proceed") is True
    assert result.get("sql") == "SELECT id FROM orders"


@pytest.mark.asyncio
async def test_reflector_receives_actionable_security_issue_without_rewriting_it() -> None:
    """Reflector should receive the detailed Validator issue as repair guidance."""
    issue = (
        "SCHEMA-002: Ensure the column exists under the referenced table; "
        "then check the table or CTE alias and qualify the column with its unique source."
    )
    candidate = Result(id=0, sql="SELECT missing_column FROM orders", prompt="schema prompt", issues=[issue])
    node = ReflectorNode(threshold=0.9)
    node.execute_with_llm_json = AsyncMock(return_value=[{"id": 0, "sql": "SELECT orders.id FROM orders"}])

    fixed_sql = await node._fix_sql([candidate])

    context = node.execute_with_llm_json.await_args.args[0]
    cases = json.loads(context.get("cases", "[]"))
    assert cases[0].get("issues", []) == [issue]
    assert fixed_sql == ["SELECT orders.id FROM orders"]
