"""Advisory SQL checks and unfinished-answer delivery for nl2sql-react."""

import json
from pathlib import Path

import pytest
from conftest import call
from langchain_core.messages import AIMessage
from test_nl2sql_react import local_db as local_db  # noqa: F401
from test_nl2sql_react_control import ANSWER, PREFIX, run, sql

from dataagent import LaunchOptions, prepare_runtime
from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/nl2sql-react"
LOADER = PythonLoader()
lint = LOADER.load(PLUGIN, "nl2sql_react/hooks/lint.py:lint")


@pytest.mark.parametrize(
    "sql_text,question,expect",
    [
        ("SELECT * FROM t", "List the rows", True),
        ("SELECT ROUND(AVG(x), 2) FROM t", "What is the average x?", True),
        ("SELECT ROUND(AVG(x), 2) FROM t", "What is the average x, rounded to 2 decimals?", False),
        ("SELECT COUNT(*) / COUNT(DISTINCT id) FROM t", "ratio", True),
        ("SELECT 1.0 * COUNT(*) / COUNT(DISTINCT id) FROM t", "ratio", False),
        (
            "SELECT COUNT(*) FROM a JOIN b ON a.id = b.a_id",
            "How many distinct customers bought?",
            True,
        ),
        (
            "SELECT COUNT(DISTINCT a.id) FROM a JOIN b ON a.id = b.a_id",
            "How many distinct customers bought?",
            False,
        ),
        ("SELECT COUNT(*) FROM a JOIN b ON a.id = b.a_id", "How many orders were placed?", False),
        ("this is not sql", "anything", False),
    ],
)
def test_lint_findings(sql_text, question, expect):
    assert bool(lint(sql_text, question)) is expect


@pytest.mark.parametrize(
    "sql_text,question,expect",
    [
        # "every ... including those with none" needs an outer join; INNER drops empty groups.
        (
            "SELECT a.id, COUNT(b.id) FROM a JOIN b ON a.id = b.a_id GROUP BY a.id",
            "Show every customer including those with no orders",
            True,
        ),
        (
            "SELECT a.id, COUNT(b.id) FROM a LEFT JOIN b ON a.id = b.a_id GROUP BY a.id",
            "Show every customer including those with no orders",
            False,
        ),
        # The request must actually mention both an inclusive scope and empty groups.
        ("SELECT a.id FROM a JOIN b ON a.id = b.a_id", "Show every customer with orders", False),
        ("SELECT a.id FROM a JOIN b ON a.id = b.a_id", "List the customers", False),
    ],
)
def test_lint_flags_inner_join_for_inclusive_requests(sql_text, question, expect):
    assert bool(lint(sql_text, question)) is expect


def saved_metadata(result):
    """metadata.json of the captured answer, as a reviewer would read it from the server."""
    artifact = next(iter(result["artifacts"]))
    return json.loads(Path(artifact).with_name("metadata.json").read_text(encoding="utf-8"))


async def test_lint_record_is_written_to_metadata(local_db, tmp_path):  # noqa: F811
    answer = "SELECT ROUND(SUM(amount), 2) FROM payments WHERE customer_id=6"
    result, _ = await run(
        configured(tmp_path, sql_lint=True),
        [AIMessage(content="LOCK_CONTRACT"), sql(answer), AIMessage(content="SELECT_RESULT")],
    )
    recorded = saved_metadata(result)["sql_lint"]
    assert recorded["enabled"] is True and recorded["reason"] == "checked"
    assert any("rounds a value" in finding for finding in recorded["findings"])


async def test_lint_record_shows_a_clean_check(local_db, tmp_path):  # noqa: F811
    # No findings is still recorded, so a saved artifact proves the check ran.
    result, _ = await run(
        configured(tmp_path, sql_lint=True),
        [
            AIMessage(content="LOCK_CONTRACT"),
            sql("SELECT COUNT(*) FROM payments"),
            AIMessage(content="SELECT_RESULT"),
        ],
    )
    recorded = saved_metadata(result)["sql_lint"]
    assert recorded == {"enabled": True, "reason": "checked", "findings": []}


async def test_lint_record_shows_disabled_and_probe_exclusions(local_db, tmp_path):  # noqa: F811
    result, _ = await run(
        configured(tmp_path, sql_lint=False),
        [
            AIMessage(content="LOCK_CONTRACT"),
            sql("SELECT ROUND(SUM(amount), 2) FROM payments WHERE customer_id=6"),
            AIMessage(content="SELECT_RESULT"),
        ],
    )
    assert saved_metadata(result)["sql_lint"]["reason"] == "disabled_by_config"
    # A probe owns its own snapshot, and it must not claim a check ran.
    result, _ = await run(
        configured(tmp_path, sql_lint=True),
        [
            AIMessage(content="REGISTER_SCHEMA"),
            sql("SELECT 1", purpose="probe", issue_id="schema"),
            AIMessage(content="LOCK_CONTRACT"),
            sql(),
            AIMessage(content="SELECT_RESULT"),
        ],
    )
    snapshots = Path(result["artifacts"][0]).parents[2].glob("queries/*/metadata.json")
    reasons = {json.loads(path.read_text())["sql_lint"]["reason"] for path in snapshots}
    assert reasons == {"checked", "not_an_answer_execution"}


def configured(tmp_path, **options):
    path = tmp_path / "config.yaml"
    config = json.loads(path.read_text())
    config["plugins"]["config"]["nl2sql-react"].update(options)
    path.write_text(json.dumps(config))
    return prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))


async def test_lint_findings_are_advisory_and_can_be_disabled(local_db, tmp_path):  # noqa: F811
    answer = "SELECT ROUND(SUM(amount), 2) FROM payments WHERE customer_id=6"
    responses = [
        AIMessage(content="LOCK_CONTRACT"),
        sql(answer),
        AIMessage(content="SELECT_RESULT"),
    ]
    result, model = await run(configured(tmp_path, sql_lint=False), responses)
    text = " ".join(
        m.text
        for request in model.requests
        for m in request
        if getattr(m, "name", "") == PREFIX + "execute_sql"
    )
    assert "sql_checks" not in text and result["status"] == "success"
    result, model = await run(configured(tmp_path, sql_lint=True), responses)
    text = " ".join(
        m.text
        for request in model.requests
        for m in request
        if getattr(m, "name", "") == PREFIX + "execute_sql"
    )
    assert "sql_checks" in text and "rounds a value" in text
    assert result["status"] == "success" and result["sql"] == answer  # Delivered unchanged.


async def test_finished_answer_is_not_delivered_without_the_option(local_db, tmp_path):  # noqa: F811
    result, _ = await run(
        configured(tmp_path, deliver_unfinished_answer=False),
        [AIMessage(content="LOCK_CONTRACT"), sql(), AIMessage(content="no finish call here")],
    )
    assert result["status"] == "error" and result["stop_reason"] == "finish_query_required"


async def test_unfinished_answer_is_delivered_only_with_the_option(local_db, tmp_path):  # noqa: F811
    runtime = configured(tmp_path, deliver_unfinished_answer=True)
    result, _ = await run(
        runtime, [AIMessage(content="LOCK_CONTRACT"), sql(), AIMessage(content="done")]
    )
    assert result["status"] == "success" and result["stop_reason"] == "auto_delivered"
    assert result["rows"] == [[117.75]] and result["final_manifest"]
    assert result["conclusion"].startswith("Delivered automatically")


async def test_unfinished_answer_is_delivered_after_a_failed_attempt(local_db, tmp_path):  # noqa: F811
    result, _ = await run(
        configured(tmp_path, deliver_unfinished_answer=True),
        [
            AIMessage(content="LOCK_CONTRACT"),
            sql("SELECT SUM(missing_amount) FROM payments WHERE customer_id=6"),
            sql(),
            AIMessage(content="done"),
        ],
    )
    evidence = result["control"]["evidence"]
    assert [e["success"] for e in evidence.values()] == [False, True]
    assert result["control"]["issues"]["execution_error"]["status"] == "resolved"
    assert result["status"] == "success" and result["stop_reason"] == "auto_delivered"
    assert result["sql"] == ANSWER and result["rows"] == [[117.75]]
    assert Path(result["final_manifest"]).is_file()


@pytest.mark.parametrize(
    "responses",
    [
        # No answer was ever executed.
        [AIMessage(content="LOCK_CONTRACT"), AIMessage(content="done")],
        # No contract was locked.
        [AIMessage(content="done")],
        # The only execution is a probe.
        [
            AIMessage(content="REGISTER_SCHEMA"),
            sql("SELECT 1", purpose="probe"),
            AIMessage(content="done"),
        ],
    ],
)
async def test_unfinished_delivery_keeps_failing_closed(local_db, tmp_path, responses):  # noqa: F811
    runtime = configured(tmp_path, deliver_unfinished_answer=True)
    result, _ = await run(runtime, responses)
    assert result["status"] == "error" and result["stop_reason"] == "finish_query_required"


async def test_unfinished_delivery_refuses_an_answer_with_an_open_issue(local_db, tmp_path):  # noqa: F811
    # The child questioned its own answer (citing the answer evidence e1) and then stopped.
    doubt = call(
        PREFIX + "update_query_state",
        {
            "issues": [
                {
                    "id": "grain",
                    "kind": "database_fact",
                    "question": "Does the result represent payment count?",
                    "acceptance": "Observe the payment rows",
                    "evidence_ids": ["e1"],
                }
            ]
        },
    )
    runtime = configured(tmp_path, deliver_unfinished_answer=True)
    result, _ = await run(
        runtime, [AIMessage(content="LOCK_CONTRACT"), sql(), doubt, AIMessage(content="done")]
    )
    assert result["status"] == "error" and result["stop_reason"] == "finish_query_required"
    assert "Does the result represent payment count?" in result["unresolved"]


async def test_unfinished_delivery_refuses_two_answers(local_db, tmp_path):  # noqa: F811
    runtime = configured(tmp_path, deliver_unfinished_answer=True)
    responses = [
        AIMessage(content="LOCK_CONTRACT"),
        sql(),
        call(
            PREFIX + "update_query_state",
            {
                "issues": [
                    {
                        "id": "rounding",
                        "kind": "database_fact",
                        "question": "Is the stored amount already rounded?",
                        "acceptance": "Observe amount values",
                        "evidence_ids": ["e1"],
                    }
                ]
            },
        ),
        sql("SELECT amount FROM payments", purpose="probe", issue_id="rounding"),
        call(
            PREFIX + "update_query_state",
            {
                "resolutions": [
                    {
                        "issue_id": "rounding",
                        "evidence_ids": ["e2"],
                        "finding": "Amounts carry two decimals",
                    }
                ]
            },
        ),
        call(
            PREFIX + "execute_sql",
            {
                "sql": "SELECT ROUND(SUM(amount), 2) FROM payments WHERE customer_id=6",
                "purpose": "answer",
                "contract_id": "CURRENT",
                "repair_issue_id": "rounding",
                "repair_reason": "Match the stored precision",
            },
        ),
        AIMessage(content="done"),
    ]
    result, _ = await run(runtime, responses)
    # Two answers exist for one contract; code must not choose between them.
    assert result["status"] == "error" and result["stop_reason"] == "finish_query_required"
