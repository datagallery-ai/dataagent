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
import pytest

from dataagent.agents.nl2sql.errors import SQLSecurityValidationError
from dataagent.agents.nl2sql.nodes.reflector import ReflectorNode
from dataagent.agents.nl2sql.workflow.state import Result, get_default_state


def _candidate(candidate_id: int, sql: str, score: float, *, blocked: bool = False) -> Result:
    violations = [{"rule_id": "FUNCTION-001", "message": "blocked"}] if blocked else []
    return Result(id=candidate_id, sql=sql, score=score, security_checked=True, security_violations=violations)


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

    assert error.value.code == "NL2SQL-SEC-001"
    assert "current_setting" not in str(error.value.detail)


@pytest.mark.asyncio
async def test_reflector_rejects_candidate_without_security_proof() -> None:
    """A resume or custom start must not bypass Validator by supplying an unchecked candidate."""
    node = ReflectorNode(threshold=0.0)
    state = get_default_state("question", ref_retries=0)
    state["validation_results"] = [Result(id=0, sql="SELECT id FROM orders", score=1.0)]

    with pytest.raises(SQLSecurityValidationError):
        await node._aprocess(state)


@pytest.mark.asyncio
async def test_reflector_explicit_false_cannot_bypass_security() -> None:
    """A stale false setting should not allow an unchecked candidate through Reflector."""
    node = ReflectorNode(threshold=0.0, sql_security_enabled=False)
    state = get_default_state("question", ref_retries=0)
    state["validation_results"] = [Result(id=0, sql="SELECT id FROM orders", score=1.0)]

    with pytest.raises(SQLSecurityValidationError):
        await node._aprocess(state)
