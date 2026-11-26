"""Control failures must terminate native tasks, not just add advice to tool text."""

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import call
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_nl2sql_react import CONTRACT, SCHEMA_ISSUE, SelectingModel
from test_nl2sql_react import local_db as local_db

from dataagent import LaunchOptions, create_agent, prepare_runtime
from dataagent.extensions.loading import PythonLoader

PREFIX = "nl2sql-react__"
BUSINESS = {
    "id": "definition",
    "kind": "business_choice",
    "question": "Ratio of totals or mean per molecule?",
    "acceptance": "The user chooses the statistic",
}
ANSWER = "SELECT SUM(amount) FROM payments WHERE customer_id=6"


def sql(text=ANSWER, *, purpose="answer", issue_id="schema"):
    args = {"sql": text, "purpose": purpose}
    args.update({"contract_id": "CURRENT"} if purpose == "answer" else {"issue_id": issue_id})
    return call(PREFIX + "execute_sql", args)


def explore():
    return call(PREFIX + "db_explore", {"question": "Total payments?", "issue_id": "schema"})


async def run(runtime, responses, *, model_type=SelectingModel, question="Total payments?"):
    model = model_type(
        responses=[
            call("task", {"subagent_type": "nl2sql-react", "description": question}),
            *responses,
            AIMessage(content="parent_done"),
        ]
    )
    async with create_agent(runtime, model=model) as agent:
        state = await agent.ainvoke({"messages": [("user", question)]})
    assert state["messages"][-1].content == "parent_done"
    assert model.cursor == len(
        model.responses
    )  # An extra child call would consume the parent reply.
    reply = next(m for m in state["messages"] if isinstance(m, ToolMessage) and m.name == "task")
    return json.loads(reply.content), model


def configure(tmp_path, **control):
    path = tmp_path / "config.yaml"
    config = json.loads(path.read_text())
    config["plugins"]["config"]["nl2sql-react"]["control"] = control
    path.write_text(json.dumps(config))
    return prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))


async def test_business_choice_ends_without_database_or_another_child_model_call(
    local_db, tmp_path
):
    result, model = await run(
        local_db[0], [call(PREFIX + "update_query_state", {"issues": [BUSINESS]})]
    )
    assert result["status"] == "needs_clarification"
    assert result["stop_reason"] == "business_choice_required" and model.cursor == 3
    assert not list(tmp_path.rglob("query.sql"))


@pytest.mark.parametrize("typed", [True, False])
async def test_exploration_business_or_unclassified_gap_is_hard_stop(local_db, typed):
    gap = (
        {"blocking_issues": [{k: BUSINESS[k] for k in ("kind", "question", "acceptance")}]}
        if typed
        else {"unresolved": [BUSINESS["question"]]}
    )
    result, _ = await run(
        local_db[0],
        [
            AIMessage(content="REGISTER_SCHEMA"),
            explore(),
            call("finish_recall", {"tables": ["payments"], **gap}),
        ],
    )
    assert result["status"] == "needs_clarification" and not result["artifacts"]
    assert BUSINESS["question"] in result["unresolved"]


async def test_database_gap_allows_one_evidenced_followup_then_finishes(local_db):
    result, _ = await run(
        local_db[0],
        [
            AIMessage(content="REGISTER_SCHEMA"),
            explore(),
            call(
                "finish_recall",
                {
                    "tables": ["payments"],
                    "blocking_issues": [
                        {
                            "kind": "database_fact",
                            "question": "Are payment values null?",
                            "acceptance": "Count null amounts",
                        }
                    ],
                },
            ),
            sql("SELECT COUNT(*) FROM payments WHERE amount IS NULL", purpose="probe"),
            call(
                PREFIX + "update_query_state",
                {
                    "contract": CONTRACT,
                    "resolutions": [
                        {
                            "issue_id": "schema",
                            "evidence_ids": ["e2"],
                            "finding": "No null payment amounts",
                        }
                    ],
                },
            ),
            sql(),
            AIMessage(content="SELECT_RESULT"),
        ],
    )
    assert result["status"] == "success" and result["rows"] == [[117.75]]
    assert result["control"]["issues"]["schema"]["attempts"] == 2
    assert result["control"]["issues"]["schema"]["status"] == "resolved"


async def test_sql_variants_cannot_extend_an_unresolved_issue(local_db, tmp_path):
    result, _ = await run(
        local_db[0],
        [
            AIMessage(content="REGISTER_SCHEMA"),
            sql("SELECT 1", purpose="probe"),
            sql("SELECT 2", purpose="probe"),
            sql("SELECT 3", purpose="probe"),
        ],
    )
    assert result["stop_reason"] == "issue_budget_exhausted"
    assert result["control"]["issues"]["schema"]["status"] == "open"
    assert len(list(tmp_path.rglob("query.sql"))) == 2


async def test_sql_fingerprint_only_supplements_issue_budget(local_db, tmp_path):
    result, _ = await run(
        local_db[0],
        [
            AIMessage(content="REGISTER_SCHEMA"),
            sql("SELECT 1", purpose="probe"),
            sql("select   1", purpose="probe"),
        ],
    )
    assert result["stop_reason"] == "duplicate_query"
    assert len(list(tmp_path.rglob("query.sql"))) == 1


async def test_new_names_cannot_reset_task_probe_budget(local_db, tmp_path):
    runtime = configure(tmp_path, max_total_probes=2)
    issues = [{**SCHEMA_ISSUE, "id": f"gap{i}", "question": f"Check fact {i}"} for i in range(3)]
    result, _ = await run(
        runtime,
        [
            call(PREFIX + "update_query_state", {"issues": issues}),
            *(sql(f"SELECT {i}", purpose="probe", issue_id=f"gap{i}") for i in range(3)),
        ],
    )
    assert (
        result["stop_reason"] == "issue_budget_exhausted" and result["control"]["total_probes"] == 2
    )


@pytest.mark.parametrize(
    "responses,reason",
    [
        ([sql("SELECT 1", purpose="probe"), sql("SELECT 2", purpose="probe")], "issue_required"),
        ([sql()], "contract_required"),
        (
            [
                AIMessage(content="LOCK_CONTRACT"),
                call(
                    PREFIX + "update_query_state",
                    {
                        "contract": {**CONTRACT, "grain": "Per customer"},
                    },
                ),
            ],
            "invalid_control_update",
        ),
        (
            [
                AIMessage(content="REGISTER_SCHEMA"),
                call(
                    PREFIX + "update_query_state",
                    {
                        "issues": [{**SCHEMA_ISSUE, "id": "renamed"}],
                    },
                ),
            ],
            "invalid_control_update",
        ),
    ],
)
async def test_protocol_violations_fail_closed(local_db, responses, reason):
    result, _ = await run(local_db[0], responses)
    assert result["status"] == "error" and result["stop_reason"] == reason


async def test_old_evidence_cannot_authorize_a_contract_change(local_db):
    result, _ = await run(
        local_db[0],
        [
            AIMessage(content="REGISTER_SCHEMA"),
            call(
                PREFIX + "search_objects",
                {"issue_id": "schema", "detail_level": "full", "pattern": "payments"},
            ),
            AIMessage(content="LOCK_CONTRACT"),
            call(
                PREFIX + "update_query_state",
                {
                    "contract": {**CONTRACT, "grain": "Per payment"},
                    "revision_evidence": ["e1"],
                    "revision_reason": "Reconsidering the old schema",
                },
            ),
        ],
    )
    assert result["stop_reason"] == "invalid_control_update"
    assert result["control"]["version"] == 1


@pytest.mark.parametrize("repeat", [False, True])
async def test_mistyped_contract_reference_gets_one_nonexecuting_correction(
    local_db, tmp_path, repeat
):
    wrong = call(
        PREFIX + "execute_sql",
        {
            "sql": ANSWER,
            "purpose": "answer",
            "contract_id": "truncated-reference",
        },
    )
    responses = [AIMessage(content="LOCK_CONTRACT"), wrong]
    responses += (
        [wrong.model_copy(deep=True)] if repeat else [sql(), AIMessage(content="SELECT_RESULT")]
    )
    result, _ = await run(local_db[0], responses)
    if repeat:
        assert result["stop_reason"] == "invalid_contract_reference"
        assert result["control"]["invalid_calls"] == 2
        assert not list(tmp_path.rglob("query.sql"))
    else:
        assert result["status"] == "success" and result["rows"] == [[117.75]]
        assert result["control"]["invalid_calls"] == 1
        assert len(list(tmp_path.rglob("query.sql"))) == 1


@pytest.mark.parametrize("stale", [False, True])
async def test_new_evidence_can_revise_once_but_old_execution_is_invalid(local_db, stale):
    class RevisionModel(SelectingModel):
        def _generate(self, messages, *args, **kwargs):
            response = super()._generate(messages, *args, **kwargs)
            for item in response.generations[0].message.tool_calls:
                if item["args"].get("execution_id") == "FIRST":
                    record = next(
                        m.artifact["nl2sql_local_result"]
                        for m in messages
                        if isinstance(m, ToolMessage)
                        and isinstance(m.artifact, dict)
                        and "nl2sql_local_result" in m.artifact
                    )
                    item["args"]["execution_id"] = record["query_id"]
            return response

    result, _ = await run(
        local_db[0],
        [
            AIMessage(content="LOCK_CONTRACT"),
            sql(),
            call(
                PREFIX + "update_query_state",
                {
                    "issues": [
                        {
                            **SCHEMA_ISSUE,
                            "id": "grain",
                            "question": "Does the result represent payment count?",
                            "evidence_ids": ["e1"],
                        }
                    ]
                },
            ),
            sql("SELECT COUNT(*) FROM payments", purpose="probe", issue_id="grain"),
            call(
                PREFIX + "update_query_state",
                {
                    "resolutions": [
                        {
                            "issue_id": "grain",
                            "evidence_ids": ["e2"],
                            "finding": "Three payment records",
                        }
                    ],
                    "contract": {**CONTRACT, "metric": "Payment count", "numerator": "COUNT(*)"},
                    "revision_evidence": ["e2"],
                    "revision_reason": "Observed rows require a count under the corrected contract",
                },
            ),
            sql("SELECT COUNT(*) FROM payments"),
            call(PREFIX + "finish_query", {"execution_id": "FIRST"})
            if stale
            else AIMessage(content="SELECT_RESULT"),
        ],
        model_type=RevisionModel,
    )
    assert result["control"]["version"] == 2
    if stale:
        assert result["stop_reason"] == "stale_execution" and result["sql"] is None
    else:
        assert result["status"] == "success" and result["rows"] == [[3]]
        assert len(result["control"]["revisions"]) == 1


@pytest.mark.parametrize(
    "enabled,target",
    [(False, {}), (True, {}), (True, {"best_effort_issue_id": BUSINESS["id"]})],
    ids=["clarify", "infer-only-open-choice", "explicit-choice"],
)
async def test_best_effort_requires_explicit_policy_and_records_assumption(
    local_db, tmp_path, enabled, target
):
    runtime = configure(tmp_path, ambiguity="best_effort_once" if enabled else "clarify")
    steps = [
        call(
            PREFIX + "update_query_state",
            {
                "issues": [BUSINESS],
                "contract": CONTRACT,
                "best_effort_assumption": "Use the supplied ratio formula; benchmark intent remains uncertain",
                **target,
            },
        )
    ]
    if enabled:
        steps += [sql(), AIMessage(content="SELECT_RESULT")]
    result, _ = await run(runtime, steps)
    if enabled:
        assert result["status"] == "partial" and result["stop_reason"] == "best_effort"
        assert result["assumptions"] and BUSINESS["question"] in result["unresolved"]
        manifest = json.loads(Path(result["final_manifest"]).read_text())
        assert (
            manifest["control"]["best_effort_used"]
            and manifest["execution_id"] == result["execution_id"]
        )
    else:
        assert result["status"] == "needs_clarification" and not result["artifacts"]


@pytest.mark.parametrize(
    "invalid_target", [{}, {"best_effort_issue_id": "unknown"}], ids=["missing-id", "unknown-id"]
)
async def test_best_effort_leaves_other_business_choices_open(local_db, tmp_path, invalid_target):
    runtime = configure(tmp_path, ambiguity="best_effort_once")
    refund = {**BUSINESS, "id": "refunds", "question": "Should this exclude refunds?"}
    assumption = "Use the ratio of totals"
    update_args = {"contract": CONTRACT, "best_effort_assumption": assumption}

    result, model = await run(
        runtime,
        [
            call(PREFIX + "update_query_state", {"issues": [BUSINESS, refund]}),
            call(PREFIX + "update_query_state", {**update_args, **invalid_target}),
            call(
                PREFIX + "update_query_state",
                {**update_args, "best_effort_issue_id": BUSINESS["id"]},
            ),
        ],
    )

    feedback = [
        json.loads(m.text)
        for m in model.requests[-2]
        if isinstance(m, ToolMessage) and m.name == PREFIX + "update_query_state"
    ][-1]
    assert feedback["retry_allowed"]
    assert feedback["error"]["code"] == "missing_business_choice"
    assert not feedback["control"]["best_effort_used"]
    assert result["status"] == "needs_clarification"
    assert result["stop_reason"] == "business_choice_required"
    assert result["unresolved"] == [refund["question"]]
    assert result["assumptions"] == [assumption]
    control = result["control"]
    assert control["issues"][BUSINESS["id"]]["status"] == "assumed"
    assert control["issues"]["refunds"]["status"] == "open"
    assert control["contract"] is None
    assert result["sql"] is None
    assert not list(tmp_path.rglob("query.sql"))


async def test_best_effort_cannot_be_used_twice(local_db, tmp_path):
    runtime = configure(tmp_path, ambiguity="best_effort_once")
    result, _ = await run(
        runtime,
        [
            call(
                PREFIX + "update_query_state",
                {
                    "issues": [BUSINESS],
                    "contract": CONTRACT,
                    "best_effort_assumption": "Choose SUM(amount) once",
                },
            ),
            sql(),
            call(
                PREFIX + "update_query_state",
                {
                    "issues": [
                        {
                            **BUSINESS,
                            "id": "another_choice",
                            "question": "Should this exclude refunds?",
                            "evidence_ids": ["e1"],
                        }
                    ],
                    "best_effort_assumption": "Assume no refunds",
                    "contract": CONTRACT,
                },
            ),
        ],
    )
    assert result["status"] == "needs_clarification" and result["control"]["best_effort_used"]
    assert result["assumptions"] == ["Choose SUM(amount) once"]


@pytest.mark.parametrize("followup", ["resolve", "repeat", "bypass"])
async def test_assumption_without_business_choice_has_one_atomic_correction(
    local_db, tmp_path, followup
):
    runtime = configure(tmp_path, ambiguity="best_effort_once")
    invalid = call(
        PREFIX + "update_query_state",
        {
            "contract": CONTRACT,
            "best_effort_assumption": "Use the observed payments",
        },
    )
    steps = [
        call(PREFIX + "update_query_state", {"issues": [SCHEMA_ISSUE]}),
        sql("SELECT COUNT(*) FROM payments", purpose="probe"),
        invalid,
    ]
    if followup == "resolve":
        steps += [
            call(
                PREFIX + "update_query_state",
                {
                    "resolutions": [
                        {
                            "issue_id": "schema",
                            "evidence_ids": ["e1"],
                            "finding": "The payments relation exists and has records",
                        }
                    ],
                    "contract": CONTRACT,
                },
            ),
            sql(),
            AIMessage(content="SELECT_RESULT"),
        ]
    elif followup == "repeat":
        steps += [invalid]
    else:
        steps += [
            call(
                PREFIX + "execute_sql",
                {
                    "sql": ANSWER,
                    "purpose": "answer",
                    "contract_id": "uncommitted_contract",
                },
            )
        ]
    result, _ = await run(runtime, steps)
    assert not result["control"]["best_effort_used"]
    assert result["assumptions"] == []
    assert result["control"]["total_probes"] == 1
    if followup == "resolve":
        assert result["status"] == "success"
        assert result["control"]["invalid_calls"] == 1
    else:
        assert result["status"] == "error"
        assert result["control"]["contract"] is None
        assert result["control"]["issues"]["schema"]["status"] == "open"
        assert not result["artifacts"]
        assert result["stop_reason"] == (
            "invalid_control_update" if followup == "repeat" else "contract_required"
        )


async def test_finish_is_required_and_success_prevents_reexecution(local_db):
    result, _ = await run(
        local_db[0],
        [
            AIMessage(content="LOCK_CONTRACT"),
            sql(),
            AIMessage(content='{"status":"success","sql":"SELECT 999"}'),
        ],
    )
    assert result["stop_reason"] == "finish_query_required" and result["sql"] is None
    result, _ = await run(
        local_db[0], [AIMessage(content="LOCK_CONTRACT"), sql(), sql("SELECT 999")]
    )
    assert result["stop_reason"] == "answer_already_executed"


async def test_parallel_child_calls_do_not_race_or_execute_database_tools(local_db, tmp_path):
    batch = AIMessage(
        content="",
        tool_calls=[
            *sql("SELECT 1", purpose="probe").tool_calls,
            *sql("SELECT 2", purpose="probe").tool_calls,
        ],
    )
    result, _ = await run(local_db[0], [AIMessage(content="REGISTER_SCHEMA"), batch])
    assert result["stop_reason"] == "parallel_control_calls" and not list(
        tmp_path.rglob("query.sql")
    )


async def test_resolving_an_auto_closed_issue_is_idempotent(local_db):
    result, _ = await run(
        local_db[0],
        [
            AIMessage(content="REGISTER_SCHEMA"),
            explore(),
            call("finish_recall", {"tables": ["payments"]}),
            call(
                PREFIX + "update_query_state",
                {
                    "contract": CONTRACT,
                    "resolutions": [
                        {
                            "issue_id": "schema",
                            "evidence_ids": ["e1"],
                            "finding": "Payment columns were observed",
                        }
                    ],
                },
            ),
            sql(),
            AIMessage(content="SELECT_RESULT"),
        ],
    )
    assert result["status"] == "success" and result["control"]["total_probes"] == 1


async def test_contract_lock_echoes_observed_findings_before_answer(local_db):
    finding = {"issue_id": "schema", "evidence_ids": ["e1"], "finding": "Observed numeric payment"}

    class CheckingModel(SelectingModel):
        def _generate(self, messages, *args, **kwargs):
            if self.cursor == 4:
                locked = json.loads(messages[-1].content)
                assert locked["resolved_findings"] == [finding]
                assert locked["contract"] == {**CONTRACT, "output_columns": []}
                assert "denominator" in locked["answer_check"]
            return super()._generate(messages, *args, **kwargs)

    result, _ = await run(
        local_db[0],
        [
            AIMessage(content="REGISTER_SCHEMA"),
            sql("SELECT amount FROM payments LIMIT 1", purpose="probe"),
            call(PREFIX + "update_query_state", {"contract": CONTRACT, "resolutions": [finding]}),
            sql(),
            AIMessage(content="SELECT_RESULT"),
        ],
        model_type=CheckingModel,
    )
    assert result["status"] == "success"


@pytest.mark.parametrize("repeat", [False, True])
async def test_answer_evidence_needs_one_repair_probe(local_db, repeat):
    issue = {
        **SCHEMA_ISSUE,
        "id": "repair",
        "evidence_ids": ["e1"],
        "question": "Did the constant zero omit recorded payments?",
    }
    resolution = {"issue_id": "repair", "evidence_ids": ["e1"], "finding": "Zero answer"}
    invalid = {"issues": [issue], "resolutions": [resolution]}

    class CheckingModel(SelectingModel):
        def _generate(self, messages, *args, **kwargs):
            if self.cursor == 4:
                rejected = json.loads(messages[-1].content)
                assert rejected["error"]["code"] == "repair_probe_required"
                assert rejected["retry_allowed"]
                assert not rejected["control"]["issues"]
            return super()._generate(messages, *args, **kwargs)

    responses = [
        AIMessage(content="LOCK_CONTRACT"),
        sql("SELECT 0 AS total"),
        call(PREFIX + "update_query_state", invalid),
    ]
    if repeat:
        responses.append(call(PREFIX + "update_query_state", invalid))
    else:
        responses.extend(
            [
                call(PREFIX + "update_query_state", {"issues": [issue]}),
                sql("SELECT amount FROM payments LIMIT 1", purpose="probe", issue_id="repair"),
                call(
                    PREFIX + "update_query_state",
                    {
                        "resolutions": [
                            {
                                **resolution,
                                "evidence_ids": ["e2"],
                                "finding": "Recorded nonzero payment",
                            },
                        ]
                    },
                ),
                call(
                    PREFIX + "execute_sql",
                    {
                        "sql": ANSWER,
                        "purpose": "answer",
                        "contract_id": "CURRENT",
                        "repair_issue_id": "repair",
                        "repair_reason": "Sum the observed payments",
                    },
                ),
                AIMessage(content="SELECT_RESULT"),
            ]
        )
    result, _ = await run(local_db[0], responses, model_type=CheckingModel)
    if repeat:
        assert result["stop_reason"] == "invalid_control_update"
        assert result["control"]["invalid_calls"] == 2 and not result["control"]["issues"]
    else:
        assert result["status"] == "success" and result["rows"] == [[117.75]]
        assert result["control"]["invalid_calls"] == 1 and result["control"]["total_probes"] == 1
        assert result["control"]["version"] == 1


@pytest.mark.parametrize("missing_reference", [False, True, "revision_only"])
async def test_specific_repair_keeps_contract_and_requires_fresh_issue_evidence(
    local_db, missing_reference
):
    result, _ = await run(
        local_db[0],
        [
            AIMessage(content="LOCK_CONTRACT"),
            sql("SELECT 0 AS total"),
            *(
                [
                    call(
                        PREFIX + "update_query_state",
                        {
                            "issues": [
                                {
                                    **SCHEMA_ISSUE,
                                    "id": "repair",
                                    "question": "Was the payment sum omitted?",
                                }
                            ],
                            **(
                                {
                                    "revision_evidence": ["e1"],
                                    "revision_reason": "Observed zero omitted the sum",
                                }
                                if missing_reference == "revision_only"
                                else {}
                            ),
                        },
                    )
                ]
                if missing_reference
                else []
            ),
            call(
                PREFIX + "update_query_state",
                {
                    "issues": [
                        {
                            **SCHEMA_ISSUE,
                            "id": "repair",
                            "question": "Was the payment sum omitted?",
                            "evidence_ids": ["e1"],
                        }
                    ]
                },
            ),
            sql("SELECT amount FROM payments LIMIT 1", purpose="probe", issue_id="repair"),
            call(
                PREFIX + "update_query_state",
                {
                    "resolutions": [
                        {
                            "issue_id": "repair",
                            "evidence_ids": ["e2"],
                            "finding": "Nonzero payment data contradicts the constant zero query",
                        }
                    ]
                },
            ),
            call(
                PREFIX + "execute_sql",
                {
                    "sql": ANSWER,
                    "purpose": "answer",
                    "contract_id": "CURRENT",
                    "repair_issue_id": "repair",
                    "repair_reason": "Replace the constant with the required SUM",
                },
            ),
            AIMessage(content="SELECT_RESULT"),
        ],
    )
    assert result["status"] == "success" and result["rows"] == [[117.75]]
    assert result["control"]["version"] == 1 and not result["control"]["revisions"]
    assert result["control"]["invalid_calls"] == int(bool(missing_reference))


@pytest.mark.parametrize("has_observation", [False, True])
async def test_missing_evidence_cannot_bypass_new_observation_or_correction_budget(
    local_db, has_observation
):
    issue = call(PREFIX + "update_query_state", {"issues": [{**SCHEMA_ISSUE, "id": "repair"}]})
    responses = [AIMessage(content="LOCK_CONTRACT")]
    if has_observation:
        responses += [sql(), issue, issue.model_copy(deep=True)]
    else:
        responses += [issue]
    result, _ = await run(local_db[0], responses)
    assert result["stop_reason"] == "invalid_control_update"
    assert result["control"]["issues"] == {}
    assert result["control"]["total_probes"] == 0
    assert result["control"]["invalid_calls"] == (2 if has_observation else 0)


async def test_execution_error_can_open_a_database_gap_but_cannot_resolve_it(local_db):
    result, _ = await run(
        local_db[0],
        [
            AIMessage(content="LOCK_CONTRACT"),
            sql("SELECT missing FROM payments"),
            call(
                PREFIX + "update_query_state",
                {"issues": [{**SCHEMA_ISSUE, "evidence_ids": ["e1"]}]},
            ),
            call(
                PREFIX + "search_objects",
                {"pattern": "payments", "detail_level": "full", "issue_id": "schema"},
            ),
            call(
                PREFIX + "update_query_state",
                {
                    "resolutions": [
                        {
                            "issue_id": "schema",
                            "evidence_ids": ["e2"],
                            "finding": "The amount column exists",
                        }
                    ]
                },
            ),
            sql(),
            AIMessage(content="SELECT_RESULT"),
        ],
    )
    assert result["status"] == "success" and result["control"]["version"] == 1


async def test_parent_reuses_blocked_task_and_can_explain_the_failure(local_db, tmp_path):
    args = {"subagent_type": "nl2sql-react", "description": "Total payments?"}
    model = SelectingModel(
        responses=[
            call("task", args),
            call(PREFIX + "update_query_state", {"issues": [BUSINESS]}),
            call("task", args),
            AIMessage(content="A choice is required."),
        ]
    )
    async with create_agent(local_db[0], model=model) as agent:
        state = await agent.ainvoke({"messages": [("user", "Total payments?")]})
    assert state["messages"][-1].content == "A choice is required."
    repeated = next(
        m
        for m in state["messages"]
        if isinstance(m, ToolMessage) and "nl2sql_parent_bypass" in (m.artifact or {})
    )
    assert json.loads(repeated.text)["status"] == "needs_clarification"
    assert model.cursor == 4 and not list(tmp_path.rglob("query.sql"))


async def test_new_user_turn_can_resume_after_clarification():
    plugin = Path(__file__).resolve().parents[1] / "dataagent/plugins/nl2sql-react"
    before = PythonLoader().load(plugin, "nl2sql_react/hooks/parent.py:before_tool")
    old = ToolMessage(
        content="blocked",
        tool_call_id="old",
        artifact={
            "nl2sql_parent_stop": {
                "user_id": "first",
                "description": "Total payments?",
                "result": {"status": "needs_clarification"},
            }
        },
    )
    request = SimpleNamespace(
        runtime=SimpleNamespace(
            state={
                "messages": [
                    HumanMessage(content="Total payments?", id="first"),
                    old,
                    HumanMessage(content="Use the ratio of totals", id="clarified"),
                ]
            }
        ),
        tool_call={
            "name": "task",
            "id": "new",
            "args": {
                "subagent_type": "nl2sql-react",
                "description": "Total payments? Use the ratio of totals",
            },
        },
    )
    assert await before(request, params={}) is None


async def test_parent_cannot_repeat_identical_sql_task(local_db):
    args = {"subagent_type": "nl2sql-react", "description": "Total payments?"}
    model = SelectingModel(
        responses=[
            call("task", {"subagent_type": "nl2sql-react", "description": "Total payments?"}),
            AIMessage(content="LOCK_CONTRACT"),
            sql(),
            AIMessage(content="SELECT_RESULT"),
            call("task", args),
            AIMessage(content="Same result."),
        ]
    )
    async with create_agent(local_db[0], model=model) as agent:
        state = await agent.ainvoke({"messages": [("user", "Total payments?")]})
    assert state["messages"][-1].text == "Same result."
    results = state["messages"][-1].additional_kwargs["nl2sql_local_results"]
    assert len(results) == 1 and results[0]["sql"] == ANSWER and model.cursor == 6
    assert any(
        isinstance(m, ToolMessage)
        and isinstance(m.artifact, dict)
        and "nl2sql_parent_bypass" in m.artifact
        for m in state["messages"]
    )


async def test_parent_sql_is_unavailable_before_delegation(local_db, tmp_path):
    model = SelectingModel(
        responses=[
            call(PREFIX + "execute_sql", {"sql": "SELECT 999", "purpose": "answer"}),
            call("task", {"subagent_type": "nl2sql-react", "description": "Total payments?"}),
            AIMessage(content="LOCK_CONTRACT"),
            sql(),
            AIMessage(content="SELECT_RESULT"),
            AIMessage(content="parent_done"),
        ]
    )
    async with create_agent(local_db[0], model=model) as agent:
        state = await agent.ainvoke({"messages": [("user", "Total payments?")]})
    rejected = next(
        m
        for m in state["messages"]
        if isinstance(m, ToolMessage) and m.name == PREFIX + "execute_sql"
    )
    assert rejected.status == "error" and "not a valid tool" in rejected.text
    assert not {PREFIX + "execute_sql", PREFIX + "search_objects"} & set(model.tool_schemas[0])
    payload = json.loads(
        next(m.text for m in state["messages"] if isinstance(m, ToolMessage) and m.name == "task")
    )
    assert payload["status"] == "success" and payload["rows"] == [[117.75]]
    assert model.cursor == 6
    assert [p.read_text() for p in tmp_path.rglob("query.sql")] == [ANSWER]


async def test_terminal_marker_still_rejects_legacy_parent_sql():
    plugin = Path(__file__).resolve().parents[1] / "dataagent/plugins/nl2sql-react"
    before = PythonLoader().load(plugin, "nl2sql_react/hooks/parent.py:before_tool")
    payload = {"status": "success", "rows": [[117.75]]}
    marker = {
        "user_id": "turn",
        "user_turn": 1,
        "description": "Total payments?",
        "result": payload,
    }
    request = SimpleNamespace(
        runtime=SimpleNamespace(
            state={
                "messages": [
                    HumanMessage(content="Total payments?", id="turn"),
                    ToolMessage(
                        content=json.dumps(payload),
                        tool_call_id="old",
                        artifact={"nl2sql_parent_stop": marker},
                    ),
                ]
            }
        ),
        tool_call={
            "name": PREFIX + "execute_sql",
            "id": "repeat",
            "args": {"sql": "SELECT 999", "purpose": "probe"},
        },
    )
    result = await before(request, params={})
    assert json.loads(result.text) == payload
    assert result.artifact["nl2sql_parent_bypass"] == marker


@pytest.mark.parametrize("evaluation", [False, True])
async def test_chat_keeps_root_answer_and_archives_local_sql(local_db, evaluation):
    question = "Total payments? Also explain what a payment is."
    delegated = "Total payments?"
    model_reply = "A payment is a transfer of funds. SELECT 999"
    model = SelectingModel(
        responses=[
            call("task", {"subagent_type": "nl2sql-react", "description": delegated}),
            AIMessage(content="LOCK_CONTRACT"),
            sql(),
            AIMessage(content="SELECT_RESULT"),
            AIMessage(content=model_reply),
        ]
    )
    cfg = {"metadata": {"evo_eval_task_id": "9"}} if evaluation else {}
    async with create_agent(local_db[0], model=model) as agent:
        state = await agent.ainvoke({"messages": [("user", question)]}, config=cfg)
    assert model.cursor == 5
    task_reply = next(
        m for m in state["messages"] if isinstance(m, ToolMessage) and m.name == "task"
    )
    assert json.loads(task_reply.text)["question"] == delegated
    last = state["messages"][-1]
    assert last.text == model_reply
    assert last.additional_kwargs["nl2sql_local_results"][0]["sql"] == ANSWER


def delegate(description):
    return call("task", {"subagent_type": "nl2sql-react", "description": description})


async def test_parent_preserves_literal_spaces_when_deduplicating_tasks(local_db):
    runtime, source, _, _ = local_db
    with sqlite3.connect(source["connection"]["path"]) as connection:
        connection.executemany("INSERT INTO customers VALUES (?, ?)", [(10, "A  B"), (11, "A B")])
    first = "Find the ID for customer named 'A  B' exactly."
    second = "Find the ID for customer named 'A B' exactly."
    model = SelectingModel(
        responses=[
            delegate(first),
            AIMessage(content="LOCK_CONTRACT"),
            sql("SELECT id FROM customers WHERE name = 'A  B'"),
            AIMessage(content="SELECT_RESULT"),
            delegate(second),
            AIMessage(content="LOCK_CONTRACT"),
            sql("SELECT id FROM customers WHERE name = 'A B'"),
            AIMessage(content="SELECT_RESULT"),
            delegate(first),
            AIMessage(content="parent_done"),
        ]
    )

    async with create_agent(runtime, model=model) as agent:
        state = await agent.ainvoke({"messages": [("user", "Look up both customer IDs.")]})

    replies = [m for m in state["messages"] if isinstance(m, ToolMessage) and m.name == "task"]
    assert [json.loads(m.text)["rows"] for m in replies] == [[[10]], [[11]], [[10]]]
    assert ["nl2sql_parent_stop" in m.artifact for m in replies] == [True, True, False]
    assert "nl2sql_parent_bypass" in replies[-1].artifact
    assert state["messages"][-1].content == "parent_done"


async def test_bounded_retries_keep_successful_sql_and_select_by_id(local_db):
    class ChooseFirstLocal(SelectingModel):
        def _generate(self, messages, *args, **kwargs):
            response = super()._generate(messages, *args, **kwargs)
            if response.generations[0].message.content == "CHOOSE_FIRST_LOCAL":
                first = next(m for m in messages if isinstance(m, ToolMessage) and m.name == "task")
                response.generations[0].message = call(
                    PREFIX + "select_result",
                    {
                        "query_id": json.loads(first.text)["query_id"],
                    },
                )
            return response

    third_sql = "SELECT ROUND(SUM(amount), 2) FROM payments WHERE customer_id=6"
    model = ChooseFirstLocal(
        responses=[
            delegate("Total payments?"),
            AIMessage(content="LOCK_CONTRACT"),
            sql(),
            AIMessage(content="SELECT_RESULT"),
            delegate("Check a different metric."),
            call(PREFIX + "update_query_state", {"issues": [BUSINESS]}),
            delegate("Total payments, rounded to two decimals?"),
            AIMessage(content="LOCK_CONTRACT"),
            sql(third_sql),
            AIMessage(content="SELECT_RESULT"),
            delegate("One more variant?"),
            delegate("Total payments?"),
            call(PREFIX + "select_result", {"query_id": "not-a-successful-query"}),
            AIMessage(content="CHOOSE_FIRST_LOCAL"),
            AIMessage(content="SELECT 999"),
        ]
    )
    async with create_agent(local_db[0], model=model) as agent:
        state = await agent.ainvoke(
            {"messages": [("user", "Total payments?")]},
            config={"metadata": {"evo_eval_task_id": "9"}},
        )
    replies = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    started = [
        m.artifact["nl2sql_parent_stop"]["result"]
        for m in replies
        if "nl2sql_parent_stop" in (m.artifact or {})
    ]
    assert [p["status"] for p in started] == ["success", "needs_clarification", "success"]
    repeated = [json.loads(m.text) for m in replies if "nl2sql_parent_bypass" in (m.artifact or {})]
    assert repeated[0]["error"]["code"] == "local_delegation_budget_exhausted"
    assert repeated[1]["query_id"] == started[0]["query_id"]
    selections = [json.loads(m.text) for m in replies if m.name == PREFIX + "select_result"]
    assert selections[0]["error"]["code"] == "invalid_local_query_selection"
    assert selections[1]["query_id"] == started[0]["query_id"]
    last = state["messages"][-1]
    assert last.text == "SELECT 999"
    assert last.additional_kwargs["nl2sql_local_selected_query_id"] == started[0]["query_id"]
    assert [p["sql"] for p in last.additional_kwargs["nl2sql_local_results"]] == [ANSWER, third_sql]
    assert model.cursor == len(model.responses)


async def test_parallel_local_tasks_cannot_consume_the_same_budget_slot(local_db):
    one = delegate("Total payments?")
    two = delegate("Count customers?")
    model = SelectingModel(
        responses=[
            AIMessage(content="", tool_calls=[*one.tool_calls, *two.tool_calls]),
            AIMessage(content="LOCK_CONTRACT"),
            sql(),
            AIMessage(content="SELECT_RESULT"),
            AIMessage(content="Done."),
        ]
    )
    async with create_agent(local_db[0], model=model) as agent:
        state = await agent.ainvoke({"messages": [("user", "Total payments and count customers?")]})
    replies = {
        m.tool_call_id: m
        for m in state["messages"]
        if isinstance(m, ToolMessage) and m.name == "task"
    }
    assert json.loads(replies[one.tool_calls[0]["id"]].text)["status"] == "success"
    rejected = replies[two.tool_calls[0]["id"]]
    assert json.loads(rejected.text)["error"]["code"] == "local_delegation_must_be_sequential"
    assert model.cursor == len(model.responses)


async def test_new_chat_turn_does_not_export_previous_local_sql(local_db):
    model = SelectingModel(
        responses=[
            delegate("Total payments?"),
            AIMessage(content="LOCK_CONTRACT"),
            sql(),
            AIMessage(content="SELECT_RESULT"),
            AIMessage(content="Payments summary."),
            AIMessage(content="A payment is a transfer of funds."),
        ]
    )
    settings = {"configurable": {"thread_id": "delivery"}}
    async with create_agent(local_db[0], model=model, checkpointer=InMemorySaver()) as agent:
        first = await agent.ainvoke({"messages": [("user", "Total payments?")]}, config=settings)
        second = await agent.ainvoke(
            {"messages": [("user", "What is a payment?")]}, config=settings
        )
    assert first["messages"][-1].additional_kwargs["nl2sql_local_results"][0]["sql"] == ANSWER
    assert second["messages"][-1].text == "A payment is a transfer of funds."
    assert not second["messages"][-1].additional_kwargs.get("nl2sql_local_results")
    assert model.cursor == 6


@pytest.mark.parametrize(
    "name,args",
    [
        ("read_file", {"path": "/outputs/data.csv"}),
        ("task", {"subagent_type": "nl2sql-react", "description": "A different data question"}),
    ],
)
async def test_successful_sql_delivery_allows_output_work_and_other_tasks(name, args):
    plugin = Path(__file__).resolve().parents[1] / "dataagent/plugins/nl2sql-react"
    before = PythonLoader().load(plugin, "nl2sql_react/hooks/parent.py:before_tool")
    question = (
        "Total payments? Also answer a different data question."
        if name == "task"
        else "Total payments?"
    )
    state = {
        "messages": [
            HumanMessage(content=question, id="user"),
            ToolMessage(
                content="done",
                tool_call_id="old",
                artifact={
                    "nl2sql_parent_stop": {
                        "user_id": "user",
                        "description": "Total payments?",
                        "result": {"status": "success"},
                    }
                },
            ),
        ]
    }
    request = SimpleNamespace(
        runtime=SimpleNamespace(state=state), tool_call={"name": name, "args": args, "id": "next"}
    )
    assert await before(request, params={}) is None


@pytest.mark.parametrize("status", ["needs_clarification", "success", "partial"])
async def test_idless_sdk_messages_do_not_carry_a_stop_into_the_next_user_turn(status):
    plugin = Path(__file__).resolve().parents[1] / "dataagent/plugins/nl2sql-react"
    loader = PythonLoader()
    after = loader.load(plugin, "nl2sql_react/hooks/parent.py:after_tool")
    before = loader.load(plugin, "nl2sql_react/hooks/parent.py:before_tool")
    state = {"messages": [HumanMessage(content="Total payments?")]}
    request = SimpleNamespace(
        runtime=SimpleNamespace(state=state),
        tool_call={
            "name": "task",
            "id": "old",
            "args": {"subagent_type": "nl2sql-react", "description": "Total payments?"},
        },
    )
    marked = await after(
        request, ToolMessage(content=json.dumps({"status": status}), tool_call_id="old"), params={}
    )
    state["messages"].append(marked)
    rejected = await before(request, params={})
    assert rejected is not None
    state["messages"].extend([rejected, HumanMessage(content="Total payments?")])
    assert await before(request, params={}) is None


@pytest.mark.parametrize("repeat", [False, True])
async def test_invalid_native_arguments_have_a_bounded_repair_budget(local_db, repeat):
    missing_sql = call(PREFIX + "execute_sql", {"purpose": "answer", "contract_id": "CURRENT"})
    responses = [AIMessage(content="LOCK_CONTRACT"), missing_sql]
    responses += (
        [missing_sql.model_copy(deep=True)]
        if repeat
        else [sql(), AIMessage(content="SELECT_RESULT")]
    )
    result, _ = await run(local_db[0], responses)
    assert result["control"]["invalid_calls"] == (2 if repeat else 1)
    assert result["status"] == ("error" if repeat else "success")
    if repeat:
        assert result["stop_reason"] == "invalid_tool_input" and not result["artifacts"]


async def test_wrong_answer_purpose_can_be_corrected_without_unregistered_database_access(
    local_db, tmp_path
):
    result, _ = await run(
        local_db[0],
        [
            AIMessage(content="LOCK_CONTRACT"),
            sql(purpose="probe"),
            sql(),
            AIMessage(content="SELECT_RESULT"),
        ],
    )
    assert result["status"] == "success" and result["rows"] == [[117.75]]
    assert result["control"]["invalid_calls"] == 1 and result["control"]["total_probes"] == 0
    assert len(list(tmp_path.rglob("query.sql"))) == 1


async def test_malformed_arguments_and_missing_issue_share_one_correction_budget(
    local_db, tmp_path
):
    result, _ = await run(
        local_db[0],
        [call(PREFIX + "execute_sql", {"purpose": "probe"}), sql("SELECT 1", purpose="probe")],
    )
    assert result["stop_reason"] == "issue_required" and result["control"]["invalid_calls"] == 2
    assert not list(tmp_path.rglob("query.sql"))


@pytest.mark.parametrize("repeat", [False, True])
async def test_unknown_issue_field_requires_removal_without_resetting_budget(
    local_db, tmp_path, repeat
):
    invalid = call(
        PREFIX + "update_query_state", {"issues": [{**SCHEMA_ISSUE, "kind_note": "database_fact"}]}
    )
    responses = [invalid]
    if repeat:
        responses.append(
            call(
                PREFIX + "update_query_state",
                {
                    "issues": [{**SCHEMA_ISSUE, "kind_hint": None}],
                },
            )
        )
    else:
        responses += [
            AIMessage(content="REGISTER_SCHEMA"),
            sql("SELECT 1", purpose="probe"),
            AIMessage(content="LOCK_CONTRACT"),
            sql(),
            AIMessage(content="SELECT_RESULT"),
        ]
    result, model = await run(local_db[0], responses)
    rejected = next(
        m
        for request in model.requests
        for m in request
        if isinstance(m, ToolMessage)
        and m.name == PREFIX + "update_query_state"
        and '"retry_allowed": true' in m.text
    )
    body = json.loads(rejected.text)
    assert "Remove unexpected fields entirely" in body["error"]["message"]
    assert "setting it to null is still invalid" in body["error"]["message"]
    assert body["control"]["issues"] == {} and body["control"]["total_probes"] == 0
    assert result["control"]["invalid_calls"] == (2 if repeat else 1)
    assert result["status"] == ("error" if repeat else "success")
    if repeat:
        assert result["stop_reason"] == "invalid_tool_input"
        assert not list(tmp_path.rglob("query.sql"))


async def test_shared_schema_supports_issue_evidence_without_resetting_its_budget(local_db):
    gap = {**SCHEMA_ISSUE, "id": "values", "question": "Are payment amounts numeric?"}
    result, _ = await run(
        local_db[0],
        [
            call(PREFIX + "update_query_state", {"issues": [SCHEMA_ISSUE, gap]}),
            call(
                PREFIX + "search_objects",
                {"issue_id": "schema", "pattern": "payments", "detail_level": "full"},
            ),
            sql("SELECT amount FROM payments LIMIT 1", purpose="probe", issue_id="values"),
            call(
                PREFIX + "update_query_state",
                {
                    "issues": [
                        {
                            **gap,
                            "question": "Numeric amounts?",
                            "acceptance": "Numeric amounts were observed",
                        }
                    ],
                    "resolutions": [
                        {
                            "issue_id": "schema",
                            "evidence_ids": ["e1"],
                            "finding": "Payment schema observed",
                        },
                        {
                            "issue_id": "values",
                            "evidence_ids": ["e1", "e2"],
                            "finding": "Numeric type and numeric observed value",
                        },
                    ],
                    "contract": CONTRACT,
                },
            ),
            sql(),
            AIMessage(content="SELECT_RESULT"),
        ],
    )
    assert result["status"] == "success"
    assert result["control"]["issues"]["values"]["attempts"] == 1
    assert result["control"]["issues"]["values"]["question"] == gap["question"]
    assert result["control"]["issues"]["values"]["acceptance"] == gap["acceptance"]


async def test_child_observes_prefetched_schema_before_direct_answer(local_db):
    class PrefetchModel(SelectingModel):
        def _generate(self, messages, *args, **kwargs):
            if self.cursor == 1:
                context = next(
                    m.content
                    for m in messages
                    if m.type == "system" and "Observed schema prefetch" in m.content
                )
                body, _ = json.JSONDecoder().raw_decode(context.split("\n", 1)[1])
                payments = next(t for t in body["tables"] if t["table"] == "payments")
                assert "amount" in payments["columns"] and not body["incomplete"]
            return super()._generate(messages, *args, **kwargs)

    result, _ = await run(
        local_db[0],
        [AIMessage(content="LOCK_CONTRACT"), sql(), AIMessage(content="SELECT_RESULT")],
        model_type=PrefetchModel,
    )
    assert result["rows"] == [[117.75]] and result["control"]["total_probes"] == 0


async def test_prefetch_directory_exposes_tables_outside_full_metadata_budget(local_db, tmp_path):
    path = tmp_path / "config.yaml"
    config = json.loads(path.read_text())
    config["plugins"]["config"]["nl2sql-react"]["dbexplore"] = {"max_tables": 1}
    path.write_text(json.dumps(config))
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))

    class DirectoryModel(SelectingModel):
        def _generate(self, messages, *args, **kwargs):
            if self.cursor == 1:
                context = next(
                    m.content
                    for m in messages
                    if m.type == "system" and "Observed schema prefetch" in m.content
                )
                body, _ = json.JSONDecoder().raw_decode(context.split("\n", 1)[1])
                assert body["table_names"] == ["customers", "payments"]
                assert body["table_names_next_offset"] is None
                assert [t["table"] for t in body["tables"]] == ["customers"]
                assert body["incomplete"]
            return super()._generate(messages, *args, **kwargs)

    result, _ = await run(
        runtime,
        [
            AIMessage(content="REGISTER_SCHEMA"),
            call(
                PREFIX + "search_objects",
                {"pattern": "payments", "detail_level": "full", "issue_id": "schema"},
            ),
            AIMessage(content="LOCK_CONTRACT"),
            sql(),
            AIMessage(content="SELECT_RESULT"),
        ],
        model_type=DirectoryModel,
    )
    assert result["rows"] == [[117.75]] and result["control"]["total_probes"] == 1


async def test_question_prefetch_prioritizes_relevant_table_with_same_budget(local_db, tmp_path):
    path = tmp_path / "config.yaml"
    config = json.loads(path.read_text())
    options = config["plugins"]["config"]["nl2sql-react"]
    options["schema_prefetch_strategy"] = "question"
    options["dbexplore"] = {"max_tables": 1}
    path.write_text(json.dumps(config))
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))

    class RankedModel(SelectingModel):
        def _generate(self, messages, *args, **kwargs):
            if self.cursor == 1:
                context = next(
                    m.content
                    for m in messages
                    if m.type == "system" and "Observed schema prefetch" in m.content
                )
                body, _ = json.JSONDecoder().raw_decode(context.split("\n", 1)[1])
                assert body["table_names"] == ["customers", "payments"]
                assert [t["table"] for t in body["tables"]] == ["payments"]
                assert "amount" in body["tables"][0]["columns"]
                assert body["incomplete"]
            return super()._generate(messages, *args, **kwargs)

    result, _ = await run(
        runtime,
        [AIMessage(content="LOCK_CONTRACT"), sql(), AIMessage(content="SELECT_RESULT")],
        model_type=RankedModel,
    )
    assert result["rows"] == [[117.75]] and result["control"]["total_probes"] == 0


def test_question_prefetch_uses_column_names_and_has_stable_no_match_fallback():
    root = Path(__file__).resolve().parents[1] / "dataagent/plugins/nl2sql-react"
    rank = PythonLoader().load(root, "nl2sql_react/hooks/context.py:rank_tables")
    inventory = ["a_history", "z_details"]
    columns = [
        {"table_name": "a_history", "column_name": "id"},
        {"table_name": "z_details", "column_name": "arrivalTime"},
    ]
    assert rank(inventory, columns, "Show arrivalTime") == ["z_details", "a_history"]
    assert rank(inventory, columns, "unknown vocabulary") == inventory
    assert rank(inventory, columns, "arrival time") == ["z_details", "a_history"]


async def test_opt_in_prefetch_samples_show_real_formats_and_clip_untrusted_text(
    local_db, tmp_path
):
    with sqlite3.connect(local_db[1]["connection"]["path"]) as conn:
        conn.execute('CREATE TABLE "date""sample" (event_date TEXT, note TEXT)')
        conn.executemany(
            'INSERT INTO "date""sample" VALUES (?, ?)',
            [
                ("2020-01-02 03:04:05", "x" * 500),
                ("2021-02-03 04:05:06", "not included"),
            ],
        )
    path = tmp_path / "config.yaml"
    config = json.loads(path.read_text())
    config["plugins"]["config"]["nl2sql-react"]["schema_sample_rows"] = 1
    path.write_text(json.dumps(config))
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))

    class SampleModel(SelectingModel):
        def _generate(self, messages, *args, **kwargs):
            if self.cursor == 1:
                context = next(
                    m.content
                    for m in messages
                    if m.type == "system" and "Observed schema prefetch" in m.content
                )
                body, _ = json.JSONDecoder().raw_decode(context.split("\n", 1)[1])
                sample = next(t["sample"] for t in body["tables"] if t["table"] == 'date"sample')
                assert sample["columns"] == ["event_date", "note"]
                assert sample["rows"] == [["2020-01-02 03:04:05", "x" * 200]]
                assert "not complete domains" in context
            return super()._generate(messages, *args, **kwargs)

    result, _ = await run(
        runtime,
        [AIMessage(content="LOCK_CONTRACT"), sql(), AIMessage(content="SELECT_RESULT")],
        model_type=SampleModel,
    )
    assert result["rows"] == [[117.75]]


async def test_native_child_receives_opt_in_literal_grounding(local_db, tmp_path):
    path = tmp_path / "config.yaml"
    config = json.loads(path.read_text())
    config["plugins"]["config"]["nl2sql-react"]["schema_value_hints"] = True
    path.write_text(json.dumps(config))
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))

    class GroundedModel(SelectingModel):
        def _generate(self, messages, *args, **kwargs):
            if self.cursor == 1:
                context = next(
                    m.content
                    for m in messages
                    if m.type == "system" and "Observed schema prefetch" in m.content
                )
                body, _ = json.JSONDecoder().raw_decode(context[context.index("{") :])
                assert body["value_hints"]["hits"] == [
                    {
                        "table": "customers",
                        "column": "name",
                        "literal": "Customer six",
                        "observed_excerpt": "Customer six",
                    }
                ]
                assert body["value_hints"]["incomplete"] is True
                assert len(json.dumps(body, ensure_ascii=False)) <= 48000
            return super()._generate(messages, *args, **kwargs)

    result, _ = await run(
        runtime,
        [AIMessage(content="LOCK_CONTRACT"), sql(), AIMessage(content="SELECT_RESULT")],
        model_type=GroundedModel,
        question="Total payments? Evidence: name = 'Customer six'",
    )
    assert result["rows"] == [[117.75]]


async def test_native_child_receives_configured_cross_database_examples(local_db, tmp_path):
    corpus = tmp_path / "examples.jsonl"
    corpus.write_text(
        json.dumps(
            {
                "db_id": "training_shop",
                "question": "Total payments?",
                "evidence": "",
                "SQL": "SELECT SUM(price) FROM purchases",
            }
        )
        + "\n"
        + json.dumps(
            {
                "db_id": "training_shop",
                "question": "What is the total amount of payments?",
                "evidence": "",
                "SQL": "SELECT SUM(price) FROM purchases",
            }
        )
    )
    path = tmp_path / "config.yaml"
    config = json.loads(path.read_text())
    config["plugins"]["config"]["nl2sql-react"]["examples"] = {
        "path": str(corpus),
        "sha256": hashlib.sha256(corpus.read_bytes()).hexdigest(),
    }
    path.write_text(json.dumps(config))
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))

    class ExampleModel(SelectingModel):
        def _generate(self, messages, *args, **kwargs):
            if self.cursor == 1:
                examples = next(
                    m.content
                    for m in messages
                    if m.type == "system" and m.content.startswith("Cross-database worked examples")
                )
                values = json.loads(examples.split("\n", 1)[1])
                assert len(values) == 1 and values[0]["database"] == "training_shop"
                assert values[0]["question"] != "Total payments?"
                assert any(
                    m.type == "system" and "Observed schema prefetch" in m.content for m in messages
                )
            return super()._generate(messages, *args, **kwargs)

    result, _ = await run(
        runtime,
        [AIMessage(content="LOCK_CONTRACT"), sql(), AIMessage(content="SELECT_RESULT")],
        model_type=ExampleModel,
    )
    assert result["rows"] == [[117.75]]


@pytest.mark.parametrize("repeat", [False, True])
async def test_malformed_json_tool_call_gets_one_repair_in_native_graph(local_db, repeat):
    invalid = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": PREFIX + "execute_sql",
                "args": '{"sql"::',
                "id": "broken",
                "error": "Invalid JSON",
            }
        ],
    )
    steps = [AIMessage(content="LOCK_CONTRACT"), invalid]
    steps += (
        [invalid.model_copy(deep=True)] if repeat else [sql(), AIMessage(content="SELECT_RESULT")]
    )
    result, _ = await run(local_db[0], steps)
    assert result["control"]["invalid_calls"] == (2 if repeat else 1)
    if repeat:
        assert result["stop_reason"] == "invalid_tool_input" and not result["artifacts"]
    else:
        assert result["status"] == "success" and result["rows"] == [[117.75]]
