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

from dataagent.agents.nl2sql.nodes.reflector import ReflectorNode
from dataagent.agents.nl2sql.workflow.state import Result, get_default_state
from dataagent.core.errors import DataAgentError


def _candidate(candidate_id: int, sql: str, score: float, *, blocked: bool = False) -> Result:
    violations = [{"rule_id": "FUNCTION-001", "message": "blocked"}] if blocked else []
    return Result(id=candidate_id, sql=sql, score=score, security_checked=True, security_violations=violations)


@pytest.mark.asyncio
async def test_reflector_selects_safe_candidate_before_scoring() -> None:
    """Reflector should never select a blocked candidate with a higher score."""
    node = ReflectorNode(threshold=0.5, sql_security_enabled=True)
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
    node = ReflectorNode(threshold=0.9, sql_security_enabled=True)
    state = get_default_state("question", ref_retries=0)
    state["validation_results"] = [_candidate(0, "SELECT current_setting('x')", 0.0, blocked=True)]

    with pytest.raises(DataAgentError) as error:
        await node._aprocess(state)

    assert error.value.source == "constraint"
    assert error.value.component == "nl2sql"
    assert "Blocked by SQL security rules" in error.value.fact
    assert "current_setting" not in error.value.fact


@pytest.mark.asyncio
async def test_reflector_rejects_candidate_without_security_proof() -> None:
    """A resume or custom start must not bypass Validator by supplying an unchecked candidate."""
    node = ReflectorNode(threshold=0.0, sql_security_enabled=True)
    state = get_default_state("question", ref_retries=0)
    state["validation_results"] = [Result(id=0, sql="SELECT id FROM orders", score=1.0)]

    with pytest.raises(DataAgentError):
        await node._aprocess(state)


@pytest.mark.asyncio
async def test_reflector_preserves_unchecked_candidates_when_security_disabled() -> None:
    """The default-off switch should preserve Reflector's pre-security candidate behavior."""
    node = ReflectorNode(threshold=0.0)
    state = get_default_state("question", ref_retries=0)
    state["validation_results"] = [Result(id=0, sql="SELECT id FROM orders", score=1.0)]

    result = await node._aprocess(state)

    assert result["proceed"] is True
    assert result["sql"] == "SELECT id FROM orders"


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
