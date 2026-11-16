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
"""The validator's half of the reflector's cross-round feedback ledger.

The repair ledger the reflector maintains (``state["review_history"]``) is
replayed to the semantic-validation prompt as ``## Prior Review Rounds``, so
the model sees each round's SQL before and after the repair plus the issues
the repairer reported ``unresolved`` — the missing half of the early-stop
design the reflector's tests pin (the first unchanged round goes back so the
validator can read the ``unresolved`` report). Verdicts that carry no issue
are normalized to score 1: repair is driven by issues, so a score below 1
with nothing to repair would send an unrepairable SQL round-tripping through
the Reflector.
"""

from nl2sql.config_manager import NL2SQLConfig
from nl2sql.nodes.validator import ValidatorNode
from nl2sql.state import Result

from nl2sql_scenarios.support import scripted_model


def make_config() -> NL2SQLConfig:
    return NL2SQLConfig(
        {
            "model": "primary",
            "database": {"db_id": "test", "dialect": "postgres"},
        }
    )


def candidate() -> Result:
    return Result(id=0, sql="SELECT bad FROM employees", score=0, issues=[])


def prior_round() -> list[dict]:
    """A ledger left by one earlier repair round (round 1 changed the SQL)."""
    return [
        {
            "round": 1,
            "id": 0,
            "sql_before": "SELECT bad FROM employees",
            "issues": ["unknown column"],
            "sql_after": "SELECT fixed FROM employees",
            "changed": True,
            "unresolved": ["no such column: bad2"],
        }
    ]


def validator_state(*, review_history: list[dict] | None = None) -> dict:
    return {
        "generation_results": [candidate()],
        "validation_results": [],
        "schema": {"employees": {"columns": {"bad": {}}}},
        "schema_str": "CREATE TABLE employees (bad INT)",
        "evidence": "",
        "question": "查询员工数",
        "sql_rules": "",
        "review_history": review_history or [],
    }


def make_validator(*responses: str) -> ValidatorNode:
    node = ValidatorNode(config=make_config())
    node.bind_model(scripted_model(*responses))
    return node


async def test_semantic_prompt_carries_review_history() -> None:
    node = make_validator('```json\n[{"id": 0, "score": 1, "issues": []}]```')
    await node.run(validator_state(review_history=prior_round()))

    user_prompt = node.model.requests[-1][1].content
    assert "## Prior Review Rounds" in user_prompt
    assert "SELECT fixed FROM employees" in user_prompt  # the seeded sql_after
    assert "no such column: bad2" in user_prompt  # and the repairer's unresolved report


async def test_semantic_prompt_omits_history_section_without_rounds() -> None:
    node = make_validator('```json\n[{"id": 0, "score": 1, "issues": []}]```')
    await node.run(validator_state())

    user_prompt = node.model.requests[-1][1].content
    assert "Prior Review Rounds" not in user_prompt


async def test_empty_issues_are_normalized_to_score_one() -> None:
    node = make_validator('```json\n[{"id": 0, "score": 0.4, "issues": []}]```')
    state = await node.run(validator_state(review_history=prior_round()))

    (result,) = state["validation_results"]
    assert result.score == 1  # nothing to repair: refuse to round-trip it
    assert result.issues == []


async def test_reported_issues_keep_their_score() -> None:
    node = make_validator('```json\n[{"id": 0, "score": 0.5, "issues": ["missing county column"]}]```')
    state = await node.run(validator_state())

    (result,) = state["validation_results"]
    assert result.score == 0.5
    assert result.issues == ["missing county column"]
