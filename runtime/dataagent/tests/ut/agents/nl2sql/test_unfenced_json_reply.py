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
"""NL2SQL JSON replies that arrive without their ```json fence, and reflector replies with a single case."""

from __future__ import annotations

import json

import pytest

from dataagent.agents.nl2sql.nodes.reflector import ReflectorNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import json_parser
from dataagent.agents.nl2sql.workflow.state import NL2SQLState, Result, get_default_state
from dataagent.core.errors import DataAgentError
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.llm_manager.adapters import LLMStreamChunk

GIVEN = "SELECT name FROM employees WHERE dept = 'engineering'"
REPAIR = "SELECT name FROM employees WHERE dept = 'Engineering'"
ISSUE = "dept values are capitalized, e.g. 'Engineering'"
CASE = {"id": 0, "sql": REPAIR}


def as_prompted(sql: str) -> str:
    """The reply the reflector prompt asks for: a one-case array in a ```json block."""
    return "```json\n" + json.dumps([{"id": 0, "sql": sql}], indent=2) + "\n```"


def lone_case(sql: str) -> str:
    return json.dumps({"id": 0, "sql": sql}, indent=2)


def one_line_array(sql: str) -> str:
    return "[\n  " + json.dumps({"id": 0, "sql": sql}) + "\n]"


UNFENCED = {
    "one case, no array": (lone_case(REPAIR), CASE),
    "array, case on one line": (one_line_array(REPAIR), [CASE]),
    "array laid out as prompted": (json.dumps([CASE], indent=2), [CASE]),
    "compact array": (json.dumps([CASE], separators=(",", ":")), [CASE]),
    "two cases": (json.dumps([CASE, {"id": 1, "sql": GIVEN}], indent=2), [CASE, {"id": 1, "sql": GIVEN}]),
    "fence opened and never closed": ("```json\n" + lone_case(REPAIR), CASE),
    "fence without a language": ("```\n" + one_line_array(REPAIR) + "\n```", [CASE]),
    "fence of another language": ("```sql\n" + lone_case(REPAIR) + "\n```", CASE),
    "blank lines around it": ("\n\n" + lone_case(REPAIR) + "\n", CASE),
}


@pytest.mark.parametrize(("reply", "expected"), UNFENCED.values(), ids=UNFENCED.keys())
def test_json_parser_reads_a_reply_that_is_only_json(reply: str, expected: object) -> None:
    assert json.loads(json_parser(reply)) == expected


def test_json_parser_keeps_backticks_inside_an_unfenced_statement() -> None:
    """MySQL quotes identifiers with backticks; inside the JSON they are not a code fence."""
    statement = "SELECT `a```, `b` FROM `t`"
    assert json.loads(json_parser(lone_case(statement))) == {"id": 0, "sql": statement}


@pytest.mark.parametrize(
    ("reply", "block"),
    [
        (as_prompted(REPAIR), json.dumps([CASE], indent=2)),
        ("Fixed below:\n" + as_prompted(REPAIR) + "\nOnly the dept value changed.", json.dumps([CASE], indent=2)),
        (lone_case(GIVEN) + "\n" + as_prompted(REPAIR), json.dumps([CASE], indent=2)),
        ("```json\n" + lone_case(REPAIR) + "\n```", lone_case(REPAIR)),
        ('```json\n[{"id": 0, "sql": "SELECT "name" FROM t"}]\n```', '[{"id": 0, "sql": "SELECT "name" FROM t"}]'),
    ],
    ids=["as prompted", "inside prose", "after other JSON", "fenced lone case", "fenced, quotes not escaped"],
)
def test_json_parser_returns_a_json_block_as_before(reply: str, block: str) -> None:
    assert json_parser(reply) == block


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "I cannot fix this SQL.",
        REPAIR,
        f"```sql\n{REPAIR}\n```",
        "Fixed below:\n" + lone_case(REPAIR),
        lone_case(REPAIR) + "\nOnly the dept value changed.",
        lone_case(REPAIR) + "\n" + lone_case(GIVEN),
        '{\n  "id": 0,\n  "sql": "SELECT "name" FROM t"\n}',
        "[Note] dept values are case-sensitive.",
    ],
    ids=[
        "empty",
        "prose",
        "bare statement",
        "sql fence",
        "prose before",
        "prose after",
        "two values",
        "quotes not escaped",
        "bracketed prose",
    ],
)
def test_json_parser_still_rejects_anything_but_json_alone(reply: str) -> None:
    with pytest.raises(DataAgentError, match="No JSON found"):
        json_parser(reply)


def _answer_every_call_with(monkeypatch: pytest.MonkeyPatch, reply: str) -> list[object]:
    """Make each model call reply ``reply``, as a model at temperature 0 tends to; return the calls."""
    calls: list[object] = []

    class _Model:
        async def astream(self, prompts):
            calls.append(prompts)
            yield LLMStreamChunk(content=reply)

    monkeypatch.setattr(llm_manager, "get_default_llm", lambda: _Model())
    return calls


async def _reflect() -> NL2SQLState:
    """Run the reflector over one candidate that needs a repair."""
    state = get_default_state("List the engineers.", ref_retries=1)
    state["validation_results"] = [Result(id=0, sql=GIVEN, prompt="schema", score=0.5, issues=[ISSUE])]
    return await ReflectorNode(threshold=0.9)._aprocess(state)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [as_prompted(REPAIR), lone_case(REPAIR), one_line_array(REPAIR), "```json\n" + lone_case(REPAIR) + "\n```"],
    ids=["as prompted", "one case, no array", "array, case on one line", "fenced lone case"],
)
async def test_reflector_takes_the_repair_from_the_first_reply(monkeypatch, reply: str) -> None:
    calls = _answer_every_call_with(monkeypatch, reply)

    state = await _reflect()

    assert [candidate.sql for candidate in state["generation_results"]] == [REPAIR]
    assert state["proceed"] is False
    assert state["validation_results"] == []
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_reflector_still_fails_on_a_reply_without_json(monkeypatch) -> None:
    calls = _answer_every_call_with(monkeypatch, "I cannot fix this SQL.")

    with pytest.raises(DataAgentError, match="No JSON found"):
        await _reflect()
    assert len(calls) == 3
