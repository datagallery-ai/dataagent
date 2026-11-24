"""Reference checker behavior without remote model or database calls."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from nl2sql.errors import DataAgentError
from nl2sql.nodes.checkers import checkers
from nl2sql.nodes.checkers.checkers import RepairError, validate_sql
from nl2sql.nodes.checkers.prompts import build_prompt, parse_result_sql

OK = (["value"], [(1,)], None)
CONTEXT = {"question": "question", "schema": "schema", "evidence": ""}


def xml_batch(*sqls):
    return [AIMessage(content=f"<result>{sql}</result>") for sql in sqls]


async def validate(sql, *, execute, ask, **options):
    return await validate_sql(sql, execute=execute, ask=ask, context=CONTEXT, **options)


@pytest.mark.parametrize("all_enabled", [False, True])
async def test_chain_invokes_selected_reference_checker_entrypoints(monkeypatch, all_enabled):
    classes = [
        checkers.SyntaxChecker,
        checkers.JoinChecker,
        checkers.OrderByLimitChecker,
        checkers.TimeChecker,
        checkers.SelectChecker,
        checkers.MaxMinChecker,
        checkers.OrderByNullChecker,
        checkers.ResultChecker,
    ]
    visited = []

    def observe(cls):
        original = cls.check_and_revise

        def traced(self, *args, **kwargs):
            visited.append(cls.__name__)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(cls, "check_and_revise", traced)

    for cls in classes:
        observe(cls)
    options = {"checkers": checkers.AVAILABLE_CHECKERS} if all_enabled else {}
    assert (
        await validate("SELECT 1", execute=AsyncMock(return_value=OK), ask=AsyncMock(), **options)
        == "SELECT 1"
    )
    expected = classes if all_enabled else [checkers.SyntaxChecker, checkers.ResultChecker]
    assert visited == [cls.__name__ for cls in expected]


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("strftime('%Y', d) = 2020", "strftime('%Y', d) = '2020'"),
        ("strftime('%Y', d) >= 202001", "strftime('%Y', d) >= '202001'"),
        ("strftime('%m', d) = 12", "strftime('%m', d) = 12"),
        ("strftime('%Y', d) = '2020'", "strftime('%Y', d) = '2020'"),
        ("STRFTIME('%Y', d) = 2020", "STRFTIME('%Y', d) = 2020"),
    ],
)
async def test_time_rule_matches_reference_case_and_digit_boundaries(expression, expected):
    repair = AsyncMock()
    sql = await validate(
        "SELECT " + expression, checkers=["time"], execute=AsyncMock(return_value=OK), ask=repair
    )
    assert sql == "SELECT " + expected
    repair.assert_not_awaited()


async def test_select_simple_concat_is_rule_only_and_preserves_other_clauses():
    repair = AsyncMock()
    before = "SELECT a || ' - ' || b FROM t WHERE x || ' ' || y = 'a b'"
    after = await validate(
        before, checkers=["select"], execute=AsyncMock(return_value=OK), ask=repair
    )
    assert after == "SELECT a, b FROM t WHERE x || ' ' || y = 'a b'"
    repair.assert_not_awaited()


async def test_select_nested_concat_and_wildcard_share_current_stage_advice():
    before = "SELECT t.*, COALESCE(a || ' ' || b, '') FROM t"
    repair = AsyncMock(return_value=xml_batch("SELECT a, b FROM t"))
    sql = await validate(
        before, checkers=["select"], execute=AsyncMock(return_value=OK), ask=repair
    )
    assert sql == "SELECT a, b FROM t"
    repair.assert_awaited_once()
    prompt = repair.await_args.args[0]
    assert f"## Previous SQL:\n{before}" in prompt
    assert "nested function calls" in prompt and "wildcard" in prompt
    assert "## Modification Suggestions:" in prompt and "## Execution Result:" not in prompt


async def test_select_wildcard_repair_uses_sql_after_rule_rewrite():
    repair = AsyncMock(return_value=xml_batch("SELECT a, b, c FROM t"))
    await validate(
        "SELECT a || ' - ' || b, t.* FROM t",
        checkers=["select"],
        execute=AsyncMock(return_value=OK),
        ask=repair,
    )
    prompt = repair.await_args.args[0]
    assert "## Previous SQL:\nSELECT a, b, t.* FROM t" in prompt
    assert "nested function calls" not in prompt


@pytest.mark.parametrize("rows", [[(1,)], [(None,)]])
async def test_max_min_single_row_skips_rewrite(rows):
    sql = "SELECT name FROM t WHERE score = (SELECT MAX(score) FROM t)"
    repair = AsyncMock()
    revised = await validate(
        sql,
        checkers=["max_min"],
        execute=AsyncMock(return_value=(["name"], rows, None)),
        ask=repair,
    )
    assert revised == sql
    repair.assert_not_awaited()


async def test_max_min_three_selects_does_not_skip_single_row():
    execute = AsyncMock(return_value=OK)
    repair = AsyncMock(return_value=xml_batch("SELECT name FROM t ORDER BY score DESC LIMIT 1"))
    sql = "SELECT name FROM t WHERE score = (SELECT MAX(score) FROM t WHERE id IN (SELECT id FROM other))"
    revised = await validate(sql, checkers=["max_min"], execute=execute, ask=repair)
    assert revised == "SELECT name FROM t ORDER BY score DESC LIMIT 1"
    repair.assert_awaited_once()
    execute.assert_not_awaited()


@pytest.mark.parametrize(
    ("candidate_result", "accepted"),
    [
        (OK, True),
        ((["value"], [], None), True),
        ((["value"], [(None,)], None), True),
        ((None, None, "still broken"), False),
    ],
)
async def test_syntax_one_candidate_requires_executable_output(candidate_result, accepted):
    repair = AsyncMock(return_value=xml_batch("SELECT repaired"))
    execute = AsyncMock(side_effect=[(None, None, "syntax error"), candidate_result])
    revised = await validate("broken", checkers=["syntax"], execute=execute, ask=repair)
    assert revised == ("SELECT repaired" if accepted else "broken")
    repair.assert_awaited_once()
    request = repair.await_args
    assert request.kwargs == {"count": 1, "stop": ["</result>"]}
    assert "## Execution Result:\nsyntax error" in request.args[0]
    assert "## Previous SQL:\nbroken" in request.args[0]


@pytest.mark.parametrize("output", [OK, (["value"], [], None), (["value"], [(None,)], None)])
async def test_syntax_accepts_empty_and_all_null_without_llm(output):
    repair = AsyncMock()
    await validate(
        "SELECT x", checkers=["syntax"], execute=AsyncMock(return_value=output), ask=repair
    )
    repair.assert_not_awaited()


async def test_result_three_samples_vote_on_row_sets_and_keep_first_tie():
    repair = AsyncMock(
        return_value=xml_batch("SELECT minority", "SELECT winner", "SELECT equivalent")
    )
    execute = AsyncMock(
        side_effect=[
            (["x"], [], None),
            (["x"], [(9,)], None),
            (["x"], [(1,), (2,)], None),
            (["x"], [(2,), (1,), (1,)], None),
        ]
    )
    revised = await validate("SELECT original", checkers=["result"], execute=execute, ask=repair)
    assert revised == "SELECT winner"
    repair.assert_awaited_once()
    assert repair.await_args.kwargs == {"count": 3, "stop": ["</result>"]}
    assert "## Previous SQL:\nSELECT original" in repair.await_args.args[0]
    assert "Case 1: Verify WHERE condition values" in repair.await_args.args[0]
    assert [call.args[0] for call in execute.await_args_list] == [
        "SELECT original",
        "SELECT minority",
        "SELECT winner",
        "SELECT equivalent",
    ]


async def test_result_all_unusable_candidates_fall_back():
    execute = AsyncMock(
        side_effect=[
            (["x"], [(None,)], None),
            (None, None, "bad"),
            (["x"], [], None),
            (["x"], [(None,)], None),
        ]
    )
    repair = AsyncMock(return_value=xml_batch("SELECT error", "SELECT empty", "SELECT null"))
    revised = await validate("SELECT original", checkers=["result"], execute=execute, ask=repair)
    assert revised == "SELECT original"


@pytest.mark.parametrize(
    ("order", "expected_requests"),
    [(["select", "syntax"], 2), (["syntax", "select"], 1)],
)
async def test_later_checker_uses_latest_sql_and_own_advice(order, expected_requests):
    before = "SELECT t.* FROM t"
    missing = "SELECT missing FROM t"
    final = "SELECT id FROM t"
    repair = AsyncMock(side_effect=[xml_batch(missing), xml_batch(final)])

    async def execute(sql):
        return (None, None, "unknown column") if sql == missing else OK

    sql = await validate(before, checkers=order, execute=execute, ask=repair)
    assert sql == (final if expected_requests == 2 else missing)
    assert len(repair.await_args_list) == expected_requests
    first = repair.await_args_list[0].args[0]
    assert f"## Previous SQL:\n{before}" in first and "wildcard" in first
    if expected_requests == 2:
        second = repair.await_args_list[1].args[0]
        assert f"## Previous SQL:\n{missing}" in second and "unknown column" in second
        assert "## Modification Suggestions:" not in second


async def test_ordinary_checker_failure_continues(monkeypatch):

    def broken(self, sql):
        raise ValueError("bad detector")

    monkeypatch.setattr(checkers.JoinChecker, "_check_join", broken)
    sql = await validate(
        "SELECT strftime('%Y', d) = 2020",
        checkers=["join", "time"],
        execute=AsyncMock(),
        ask=AsyncMock(),
    )
    assert sql.endswith("= '2020'")


@pytest.mark.parametrize("error", [RepairError("parse exhausted"), RuntimeError("LLM exhausted")])
async def test_repair_failure_aborts_chain(error):
    with pytest.raises(RepairError):
        await validate(
            "broken",
            checkers=["syntax", "time"],
            execute=AsyncMock(return_value=(None, None, "bad")),
            ask=AsyncMock(side_effect=error),
        )


async def test_cancellation_propagates():
    with pytest.raises(asyncio.CancelledError):
        await validate(
            "SELECT x",
            checkers=["syntax"],
            execute=AsyncMock(side_effect=asyncio.CancelledError),
            ask=AsyncMock(),
        )


@pytest.mark.parametrize(
    ("result_type", "guidance"),
    [
        ("empty_result", "Case 1: Verify WHERE condition values"),
        ("all_null_result", "Case 1: Verify aggregate functions over an empty input set"),
    ],
)
def test_execution_prompt_separates_diagnosis_from_common_constraints(result_type, guidance):
    context = {
        "id": 7,
        "schema": "SCHEMA",
        "question": "QUESTION",
        "evidence": "HINT",
        "sql_guidance": "PRIVATE GUIDANCE",
    }
    prompt = build_prompt(
        context, "SELECT previous", "execution", ["OTHER ADVICE"], "RESULT", result_type
    )
    assert guidance in prompt
    assert "PRIVATE GUIDANCE" not in prompt and "OTHER ADVICE" not in prompt
    assert "NOT ALLOWED" not in prompt and "Modification Suggestions" not in prompt
    assert "## Execution Result:\nRESULT" in prompt
    assert "<result>" in prompt and "<reasoning>" in prompt


def test_common_prompt_preserves_guidance_and_scope_constraint():
    prompt = build_prompt(
        {"schema": "SCHEMA", "sql_guidance": "GUIDANCE"},
        "SELECT original",
        "common",
        ["CURRENT ADVICE"],
        "PRIVATE RESULT",
    )
    assert "GUIDANCE" in prompt and "CURRENT ADVICE" in prompt
    assert "NOT ALLOWED" in prompt and "Execution Result" not in prompt
    assert "PRIVATE RESULT" not in prompt and "<result>" in prompt


async def test_result_partial_samples_still_participate_after_parse_budget_exhaustion():
    repair = AsyncMock(
        side_effect=[
            [
                *xml_batch("SELECT partial"),
                AIMessage(content="invalid"),
                AIMessage(content="invalid"),
            ],
            [AIMessage(content="invalid"), AIMessage(content="invalid")],
        ]
    )
    execute = AsyncMock(side_effect=[(["x"], [], None), OK])
    revised = await validate(
        "SELECT original", checkers=["result"], execute=execute, ask=repair, max_parse_retries=1
    )
    assert revised == "SELECT partial"
    assert [r.kwargs["count"] for r in repair.await_args_list] == [3, 2]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("prefix <result>SELECT 1", "SELECT 1"),
        ("<reasoning>x</reasoning>\n[result>```sql\nSELECT 1\n```", "SELECT 1"),
        ("<result>```\nSELECT 3\n```</result><result>SELECT 4</result>", "SELECT 3"),
        ("```sql\nSELECT 1\n```", None),
        ("<result>   </result>", None),
    ],
)
def test_reference_reply_parser(response, expected):
    assert parse_result_sql(response) == expected


@pytest.mark.parametrize("kind", ["common", "execution"])
def test_no_explicit_reasoning_mode(kind):
    prompt = build_prompt({"emit_reasoning_tag": False}, "SELECT 1", kind, ["advice"], "error")
    assert "<reasoning>" not in prompt and "<result>" in prompt


def test_result_feedback_table():
    assert checkers._format_execution((["a"], [(None,)], None)) == (
        "+-----+\n| a   |\n|-----|\n|     |\n+-----+"
    )


@pytest.mark.parametrize("callback", ["execute", "repair"])
async def test_infrastructure_error_preserves_typed_exception(callback):
    failure = DataAgentError(source="tool", component="nl2sql", fact="driver unavailable")
    execute = AsyncMock(return_value=(None, None, "invalid SQL"))
    repair = AsyncMock()
    {"execute": execute, "repair": repair}[callback].side_effect = failure
    with pytest.raises(DataAgentError) as caught:
        await validate("SELECT missing", execute=execute, ask=repair)
    assert caught.value is failure


@pytest.mark.parametrize("kind", ["common", "execution"])
@pytest.mark.parametrize(
    "dialect,label", [("sqlite", "SQLite"), ("mysql", "MySQL"), ("spark", "spark")]
)
def test_both_repair_templates_name_the_configured_dialect(kind, dialect, label):
    prompt = build_prompt({"dialect": dialect}, "SELECT 1", kind, ["advice"], "error")
    assert f"executed by {label} directly" in prompt
    assert f"any {label} comment" in prompt
