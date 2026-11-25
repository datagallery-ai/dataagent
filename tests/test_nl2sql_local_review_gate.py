"""Code-enforced review uses a scripted reviewer and a real SQLite execution path."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage, ToolMessage
from test_nl2sql_local import CONTRACT, SCHEMA_ISSUE, SelectingModel
from test_nl2sql_local import local_db as local_db  # noqa: F401
from test_nl2sql_local_control import PREFIX, run, sql
from test_nl2sql_local_lint_delivery import configured, saved_metadata

from dataagent import create_agent
from dataagent.extensions.loading import PythonLoader

ANSWER = "SELECT SUM(amount) AS total FROM payments WHERE customer_id=6"
CONTRACT_V4 = {**CONTRACT, "output_columns": ["total"]}


def lock(draft=ANSWER, contract=None):
    return call(
        PREFIX + "update_query_state", {"contract": contract or CONTRACT_V4, "draft_sql": draft}
    )


def reviewer(
    monkeypatch,
    *,
    answer=ANSWER,
    contract=None,
    changes=None,
    unresolved=None,
    error=None,
    malformed=False,
    timeout_attempts=0,
    unresolved_attempts=None,
):
    requests = []

    def structured(self, schema, **kwargs):
        async def invoke(messages, **kwargs):
            requests.append(messages)
            if len(requests) <= timeout_attempts:
                await asyncio.sleep(60)
            if error:
                raise error
            if malformed:
                return {"parsed": None, "parsing_error": ValueError("invalid review")}
            pending = (
                unresolved
                if unresolved_attempts is None or len(requests) <= unresolved_attempts
                else None
            )
            value = schema.model_validate(
                {
                    "sql": answer,
                    "contract": contract or CONTRACT_V4,
                    "changes": changes or [],
                    "unresolved": pending or [],
                }
            )
            return {"parsed": value, "raw": AIMessage(content="scripted review")}

        return SimpleNamespace(ainvoke=invoke)

    monkeypatch.setattr(ScriptedModel, "with_structured_output", structured)
    return requests


def enabled(tmp_path, **options):
    return configured(tmp_path, review_sql=True, deliver_unfinished_answer=True, **options)


def test_ver4_defaults_enable_integrated_features():
    plugin = Path(__file__).resolve().parents[1] / "dataagent/plugins/nl2sql-react"
    config = PythonLoader().load(plugin, "nl2sql_local/tools/database.py:Config")(
        database={"path": "/unused.sqlite", "db_id": "fixture"}
    )
    assert (
        config.review_sql
        and config.sql_lint
        and config.projection_check
        and config.deliver_unfinished_answer
    )
    assert config.review_timeout_seconds == 180 and config.review_max_retries == 3


async def test_automatic_review_and_delivery_without_either_explicit_tool(
    local_db, tmp_path, monkeypatch
):  # noqa: F811
    calls = reviewer(monkeypatch)
    result, _ = await run(enabled(tmp_path), [lock(), sql(ANSWER), AIMessage(content="done")])
    assert len(calls) == 1
    assert result["status"] == "success" and result["stop_reason"] == "auto_delivered"
    assert result["sql"] == ANSWER and result["rows"] == [[117.75]]
    assert result["review"]["status"] == "reviewed" and not result["review"]["repair_used"]
    assert result["review_applies_to_selected_sql"] is True
    assert json.loads(Path(result["final_manifest"]).read_text())["stop_reason"] == "auto_delivered"
    assert saved_metadata(result)["sql_lint"]["reason"] == "checked"


@pytest.mark.parametrize(
    ("rules", "mode"),
    [
        (None, "critique"),
        ("sql_rules_bird", "critique"),
        ("sql_rules_bird", "independent"),
    ],
)
async def test_configured_rules_reach_local_and_automatic_review(
    local_db, tmp_path, monkeypatch, rules, mode
):  # noqa: F811
    calls = reviewer(monkeypatch)
    options = {"user_sql_rules": rules} if rules else {}
    runtime = enabled(tmp_path, schema_prefetch=False, review_mode=mode, **options)
    result, model = await run(runtime, [lock(), sql(ANSWER), AIMessage(content="SELECT_RESULT")])
    assert result["status"] == "success" and len(calls) == 1

    marker = "Additional SQL guidance (sql_rules_bird)."
    assert all(marker not in message.text for message in model.requests[0])
    child_rules = [message.text for message in model.requests[1] if marker in message.text]
    review_prompt = calls[0][0].text
    if rules is None:
        assert not child_rules and marker not in review_prompt
    else:
        (rule_prompt,) = child_rules
        assert "Handling NULLs" in rule_prompt
        assert review_prompt.endswith(rule_prompt)


async def test_one_review_repairs_sql_and_contract_before_any_execution(
    local_db, tmp_path, monkeypatch
):  # noqa: F811
    draft = "SELECT ROUND(SUM(amount), 0) AS total, COUNT(*) AS n FROM payments WHERE customer_id=6"
    calls = reviewer(
        monkeypatch, changes=["Remove unrequested rounding and extra count"], contract=CONTRACT_V4
    )
    result, _ = await run(
        enabled(tmp_path),
        [
            lock(draft, {**CONTRACT_V4, "numerator": "ROUND(SUM(amount), 0)"}),
            sql(ANSWER),
            AIMessage(content="SELECT_RESULT"),
        ],
    )
    assert len(calls) == 1 and result["status"] == "success"
    assert result["sql"] == ANSWER and result["rows"] == [[117.75]]
    assert result["review"]["repair_used"] is True
    assert "diagnostics" not in result["review"]
    assert result["control"]["contract"]["numerator"] == "SUM(amount)"
    request = json.loads(calls[0][-1].content)
    assert request["checks"]["sql_lint"] and "n" in request["checks"]["projection_check"]
    assert len(list(tmp_path.rglob("query.sql"))) == 1


@pytest.mark.parametrize("repeat", [False, True])
async def test_unreviewed_sql_cannot_bypass_gate(local_db, tmp_path, monkeypatch, repeat):  # noqa: F811
    calls = reviewer(monkeypatch)
    responses = [lock(), sql("SELECT COUNT(*) FROM payments")]
    responses += (
        [sql("SELECT COUNT(*) FROM payments")]
        if repeat
        else [sql(ANSWER), AIMessage(content="SELECT_RESULT")]
    )
    result, _ = await run(enabled(tmp_path), responses)
    assert len(calls) == 1
    if repeat:
        assert result["stop_reason"] == "reviewed_sql_required"
        assert not list(tmp_path.rglob("query.sql"))
    else:
        assert result["rows"] == [[117.75]]
        assert len(list(tmp_path.rglob("query.sql"))) == 1


async def test_missing_draft_can_be_corrected_without_spending_review(
    local_db, tmp_path, monkeypatch
):  # noqa: F811
    calls = reviewer(monkeypatch)
    result, _ = await run(
        enabled(tmp_path),
        [
            call(PREFIX + "update_query_state", {"contract": CONTRACT_V4}),
            lock(),
            sql(ANSWER),
            AIMessage(content="SELECT_RESULT"),
        ],
    )
    assert len(calls) == 1 and result["status"] == "success"
    assert result["control"]["invalid_calls"] == 1


@pytest.mark.parametrize("mode", ["unresolved", "timeout", "malformed"])
async def test_failed_review_never_locks_executes_or_auto_delivers(
    local_db, tmp_path, monkeypatch, mode
):  # noqa: F811
    calls = reviewer(
        monkeypatch,
        unresolved=["Which population?"] if mode == "unresolved" else [],
        error=TimeoutError("review timed out") if mode == "timeout" else None,
        malformed=mode == "malformed",
    )
    result, _ = await run(enabled(tmp_path), [lock()])
    assert len(calls) == (4 if mode == "timeout" else 1)
    assert result["review"]["attempts"] == len(calls)
    assert result["stop_reason"] == (
        "review_unresolved" if mode == "unresolved" else "review_failed"
    )
    assert result["control"]["contract"] is None
    assert not list(tmp_path.rglob("query.sql"))


async def test_review_retries_same_request_after_timeout(local_db, tmp_path, monkeypatch):  # noqa: F811
    calls = reviewer(monkeypatch, timeout_attempts=3)
    runtime = enabled(tmp_path, review_timeout_seconds=0.02)
    result, _ = await run(runtime, [lock(), sql(ANSWER), AIMessage(content="SELECT_RESULT")])
    assert len(calls) == 4 and all(messages == calls[0] for messages in calls)
    assert result["review"]["attempts"] == 4
    assert result["rows"] == [[117.75]]


@pytest.mark.parametrize("evaluation", [False, True])
@pytest.mark.parametrize("root_recovers", [False, True])
async def test_root_answer_is_preserved_after_local_failure(
    local_db,
    tmp_path,
    monkeypatch,
    evaluation,
    root_recovers,  # noqa: F811
):
    reviewer(monkeypatch, error=TimeoutError("review timed out"))
    question = "Total payments?"
    root_reply = AIMessage(
        content=ANSWER
        if root_recovers
        else json.dumps(
            {
                "status": "error",
                "sql": None,
                "query_id": None,
                "execution_id": None,
                "rows": [],
                "artifacts": [],
                "final_manifest": None,
                "error": {"code": "review_failed", "message": "Unable to finish with the draft"},
                "review": {"input_sql": ANSWER},
            }
        ),
    )
    model = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "nl2sql-react", "description": question}),
            lock(),
            root_reply,
        ]
    )
    config = {"metadata": {"evo_eval_task_id": "42"}} if evaluation else {}
    async with create_agent(enabled(tmp_path, review_max_retries=0), model=model) as agent:
        result = await agent.ainvoke({"messages": [("user", question)]}, config=config)

    assert model.cursor == 3
    tool_reply = next(
        m for m in model.requests[-1] if isinstance(m, ToolMessage) and m.name == "task"
    )
    assert json.loads(tool_reply.content)["review"]["input_sql"] == ANSWER
    assert tool_reply in result["messages"]
    last = result["messages"][-1]
    assert last.content == root_reply.content
    assert not last.additional_kwargs["nl2sql_local_results"]
    assert last.additional_kwargs["nl2sql_local_failure"]["error"]["code"] == "review_failed"


async def test_root_can_resolve_review_gap_by_redelegating(local_db, tmp_path, monkeypatch):  # noqa: F811
    calls = reviewer(
        monkeypatch, unresolved=["Which customer's payments count?"], unresolved_attempts=1
    )
    question = "What is the total of payments for customer 6?"
    model = SelectingModel(
        responses=[
            call(
                "task",
                {"subagent_type": "nl2sql-react", "description": "What is the total of payments?"},
            ),
            lock(),
            call("task", {"subagent_type": "nl2sql-react", "description": question}),
            lock(),
            sql(ANSWER),
            AIMessage(content="done"),
            AIMessage(content="SELECT 999"),
        ]
    )
    async with create_agent(enabled(tmp_path), model=model) as agent:
        result = await agent.ainvoke(
            {"messages": [("user", question)]}, config={"metadata": {"evo_eval_task_id": "42"}}
        )

    replies = [
        json.loads(m.text)
        for m in result["messages"]
        if isinstance(m, ToolMessage) and m.name == "task"
    ]
    assert len(calls) == 2 and len(replies) == 2
    assert replies[0]["status"] == "needs_clarification" and replies[0]["sql"] is None
    assert replies[0]["review"]["sql"] == ANSWER
    assert replies[1]["status"] == "success" and replies[1]["rows"] == [[117.75]]
    assert [p.read_text() for p in tmp_path.rglob("query.sql")] == [ANSWER]
    last = result["messages"][-1]
    assert last.text == "SELECT 999"
    assert last.additional_kwargs["nl2sql_local_selected_query_id"] == replies[1]["query_id"]


async def test_explicit_review_is_reused_by_lock(local_db, tmp_path, monkeypatch):  # noqa: F811
    calls = reviewer(monkeypatch)
    result, _ = await run(
        enabled(tmp_path),
        [
            call(PREFIX + "review_query", {"sql": ANSWER, "contract": CONTRACT_V4}),
            lock(),
            sql(ANSWER),
            AIMessage(content="SELECT_RESULT"),
        ],
    )
    assert len(calls) == 1 and result["status"] == "success"


async def test_changed_draft_cannot_buy_a_second_review(local_db, tmp_path, monkeypatch):  # noqa: F811
    calls = reviewer(monkeypatch)
    result, _ = await run(
        enabled(tmp_path),
        [
            call(PREFIX + "review_query", {"sql": ANSWER, "contract": CONTRACT_V4}),
            lock("SELECT COUNT(*) FROM payments"),
        ],
    )
    assert len(calls) == 1 and result["stop_reason"] == "review_failed"
    assert not list(tmp_path.rglob("query.sql"))


@pytest.mark.parametrize(("sql_changed", "contract_changed"), [(True, False), (False, True)])
async def test_repair_without_reason_is_diagnostic(
    local_db, tmp_path, monkeypatch, sql_changed, contract_changed
):  # noqa: F811
    calls = reviewer(monkeypatch)
    draft = "SELECT COUNT(*) FROM payments" if sql_changed else ANSWER
    contract = {**CONTRACT_V4, "numerator": "COUNT(*)"} if contract_changed else CONTRACT_V4
    result, _ = await run(
        enabled(tmp_path), [lock(draft, contract), sql(ANSWER), AIMessage(content="SELECT_RESULT")]
    )
    assert len(calls) == 1 and result["status"] == "success"
    assert result["rows"] == [[117.75]]
    assert result["review"]["diagnostics"] == [
        {
            "code": "missing_change_reasons",
            "sql_changed": sql_changed,
            "contract_changed": contract_changed,
        }
    ]
    assert (
        json.loads(Path(result["final_manifest"]).read_text())["review"]["diagnostics"]
        == result["review"]["diagnostics"]
    )


async def test_lint_remains_advisory_after_semantic_review(local_db, tmp_path, monkeypatch):  # noqa: F811
    draft = "SELECT ROUND(SUM(amount), 2) AS total FROM payments WHERE customer_id=6"
    calls = reviewer(monkeypatch, answer=draft)
    result, _ = await run(
        enabled(tmp_path), [lock(draft), sql(draft), AIMessage(content="SELECT_RESULT")]
    )
    assert len(calls) == 1 and result["status"] == "success"
    assert result["review"]["checks_after"]["sql_lint"]
    assert result["sql"] == draft and not result["review"]["repair_used"]


async def test_execution_error_keeps_the_existing_bounded_repair_path(
    local_db, tmp_path, monkeypatch
):  # noqa: F811
    draft = "SELECT SUM(missing_amount) AS total FROM payments WHERE customer_id=6"
    calls = reviewer(monkeypatch, answer=draft)
    result, _ = await run(
        enabled(tmp_path),
        [lock(draft), sql(draft), sql(ANSWER), AIMessage(content="SELECT_RESULT")],
    )
    assert len(calls) == 1 and result["status"] == "success"
    assert result["sql"] == ANSWER and result["rows"] == [[117.75]]
    assert result["control"]["issues"]["execution_error"]["status"] == "resolved"
    assert result["review_applies_to_selected_sql"] is False


async def test_resolved_findings_reach_enforced_review(local_db, tmp_path, monkeypatch):  # noqa: F811
    calls = reviewer(monkeypatch)
    resolution = {
        "issue_id": "schema",
        "evidence_ids": ["e1"],
        "finding": "Three payment records exist",
    }
    locked = call(
        PREFIX + "update_query_state",
        {"contract": CONTRACT_V4, "draft_sql": ANSWER, "resolutions": [resolution]},
    )
    result, _ = await run(
        enabled(tmp_path),
        [
            AIMessage(content="REGISTER_SCHEMA"),
            sql("SELECT COUNT(*) FROM payments", purpose="probe"),
            locked,
            sql(ANSWER),
            AIMessage(content="SELECT_RESULT"),
        ],
    )
    assert result["status"] == "success" and len(calls) == 1
    request = json.loads(calls[0][-1].content)
    assert request["resolved_findings"][0]["finding"] == resolution["finding"]
    assert request["resolved_findings"][0]["evidence_ids"] == ["e1"]


async def test_new_evidence_invalidates_manual_review_without_new_budget(
    local_db, tmp_path, monkeypatch
):  # noqa: F811
    calls = reviewer(monkeypatch)
    locked = call(
        PREFIX + "update_query_state",
        {
            "contract": CONTRACT_V4,
            "draft_sql": ANSWER,
            "resolutions": [
                {"issue_id": "schema", "evidence_ids": ["e1"], "finding": "Observed payments"}
            ],
        },
    )
    result, _ = await run(
        enabled(tmp_path),
        [
            call(PREFIX + "review_query", {"sql": ANSWER, "contract": CONTRACT_V4}),
            call(PREFIX + "update_query_state", {"issues": [SCHEMA_ISSUE]}),
            sql("SELECT COUNT(*) FROM payments", purpose="probe"),
            locked,
        ],
    )
    assert len(calls) == 1 and result["stop_reason"] == "review_failed"
    assert result["review"]["error"]["code"] == "review_budget_exhausted"
