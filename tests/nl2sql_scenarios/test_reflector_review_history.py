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
"""The reflector's repair ledger (``review_history``) and its early-stop rule.

Each repair round is recorded in ``state["review_history"]`` and replayed to
the fix prompt as <review_history>, so the model does not undo earlier
repairs and can report issues it cannot fix as ``unresolved``. When a round
produces no SQL change for any candidate — and at least one prior round
exists, a safe best exists and it is not flagged ``need_ref`` — the reflector
accepts the best candidate instead of burning the remaining ``ref_retries``
on identical SQL.
"""

from nl2sql.config_manager import NL2SQLConfig
from nl2sql.nodes.reflector import ReflectorNode
from nl2sql.state import Result

from nl2sql_scenarios.support import scripted_model


def make_config() -> NL2SQLConfig:
    return NL2SQLConfig(
        {
            "model": "primary",
            "database": {"db_id": "test", "dialect": "postgres"},
        }
    )


def failed() -> Result:
    return Result(id=0, sql="SELECT bad FROM employees", score=0.3, issues=["unknown column"])


def same_sql_response() -> str:
    return '```json\n[{"id": 0, "sql": "SELECT bad FROM employees"}]```'


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
            "unresolved": [],
        }
    ]


def reflector_state(result: Result, *, review_history: list[dict] | None = None) -> dict:
    return {
        "validation_results": [result],
        "generation_results": [],
        "ref_retries": 2,
        "proceed": False,
        "security_sql_approved": False,
        "review_history": review_history or [],
    }


def make_reflector(*responses: str) -> ReflectorNode:
    node = ReflectorNode(config=make_config(), threshold=0.9)
    node.bind_model(scripted_model(*responses))
    return node


async def test_changed_repair_records_history_round() -> None:
    node = make_reflector(
        '```json\n[{"id": 0, "sql": "SELECT repaired FROM employees", "unresolved": ["no such column: bad2"]}]```'
    )
    state = await node.run(reflector_state(failed()))

    assert state["proceed"] is False  # the repair changed the SQL: another validation round
    assert [r.sql for r in state["generation_results"]] == ["SELECT repaired FROM employees"]
    (entry,) = state["review_history"]
    assert entry["changed"] is True
    assert entry["sql_before"] == "SELECT bad FROM employees"
    assert entry["sql_after"] == "SELECT repaired FROM employees"
    assert entry["unresolved"] == ["no such column: bad2"]


async def test_first_unchanged_round_still_goes_back() -> None:
    node = make_reflector(same_sql_response())
    state = await node.run(reflector_state(failed()))  # no prior round yet

    assert (
        state["proceed"] is False
    )  # first unchanged round: one more pass so the validator reads `unresolved`
    (entry,) = state["review_history"]
    assert entry["changed"] is False
    assert [r.sql for r in state["generation_results"]] == ["SELECT bad FROM employees"]


async def test_second_unchanged_round_accepts_early() -> None:
    node = make_reflector(same_sql_response())
    state = await node.run(reflector_state(failed(), review_history=prior_round()))

    assert (
        state["proceed"] is True
    )  # score 0.3 < threshold 0.9, yet accepted: no progress left to make
    assert state["sql"] == "SELECT bad FROM employees"
    assert [r.sql for r in state["validation_results"]] == ["SELECT bad FROM employees"]
    assert len(state["review_history"]) == 2  # the stopping round is recorded before accepting


async def test_unchanged_round_with_flagged_candidate_keeps_looping() -> None:
    flagged = Result(
        id=0, sql="SELECT bad FROM employees", score=0.3, issues=["unknown column"], need_ref=True
    )
    node = make_reflector(same_sql_response())
    state = await node.run(reflector_state(flagged, review_history=prior_round()))

    assert (
        state["proceed"] is False
    )  # need_ref (dimension join) must burn its retries, progress or not
    assert [r.sql for r in state["generation_results"]] == ["SELECT bad FROM employees"]
    # the repair resets the flag; re-flagging is the post_sql hook's business
    assert all(not result.need_ref for result in state["generation_results"])


async def test_failed_fix_records_no_history() -> None:
    wrong_length = '```json\n[{"id": 0, "sql": "SELECT a"}, {"id": 1, "sql": "SELECT b"}]```'
    node = make_reflector(wrong_length, wrong_length, wrong_length)
    state = await node.run(reflector_state(failed(), review_history=prior_round()))

    assert state["proceed"] is False
    assert state["review_history"] == prior_round()  # a failed fix is not an unchanged round
    assert [r.sql for r in state["generation_results"]] == ["SELECT bad FROM employees"]


async def test_fix_prompt_carries_review_history() -> None:
    node = make_reflector('```json\n[{"id": 0, "sql": "SELECT repaired FROM employees"}]```')
    await node.run(reflector_state(failed(), review_history=prior_round()))

    user_prompt = node.model.requests[-1][1].content
    assert (
        "SELECT fixed FROM employees" in user_prompt
    )  # the seeded sql_after, rendered into <review_history>
