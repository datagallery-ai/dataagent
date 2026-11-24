"""Exercise the BIRD node's XML/model and database boundary."""

import sqlite3
from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage
from nl2sql.config_manager import NL2SQLConfig
from nl2sql.errors import DataAgentError
from nl2sql.nodes.bird_validator import BirdValidatorNode
from nl2sql.state import Result, get_default_state
from nl2sql.utils.sql_service import SQLiteService


class Model:
    def __init__(self, replies, **extra_body):
        self.replies = iter(replies)
        self.requests = []
        self.extra_body = extra_body

    async def astream(self, messages, **kwargs):
        self.requests.append((messages, kwargs))
        yield AIMessage(content=next(self.replies))


def node(tmp_path, replies, **extra):
    path = tmp_path / "db.sqlite"
    sqlite3.connect(path).close()
    node = BirdValidatorNode(
        config=NL2SQLConfig(
            {
                "model": "test",
                "database": {"dialect": "sqlite", "path": str(path)},
            }
        ),
        repair_attempts=1,
    )
    node.bind_model(Model(replies, **extra))
    return node


async def test_real_syntax_repair_shares_final_execution_and_preserves_order(tmp_path, monkeypatch):
    validator = node(tmp_path, ["<reasoning>column is missing</reasoning><result>SELECT 42"])
    state = get_default_state(
        "return 42",
        generation_results=[
            Result(9, "SELECT missing_column"),
            Result(5, "SELECT 42"),
        ],
    )
    executed = []
    execute = validator._execute

    def count(sql, schema):
        executed.append(sql)
        return execute(sql, schema)

    monkeypatch.setattr(validator, "_execute", count)
    final = await validator.run(state)
    assert [(r.id, r.sql, r.rows_preview) for r in final["execution_results"]] == [
        (9, "SELECT 42", [(42,)]),
        (5, "SELECT 42", [(42,)]),
    ]
    assert executed.count("SELECT 42") == 1
    [(messages, kwargs)] = validator.model.requests
    assert len(messages) == 1 and "<reasoning>" in messages[0].content
    assert "missing_column" in messages[0].content
    assert kwargs["stop"] == ["</result>"] and kwargs["temperature"] == 0.3


@pytest.mark.parametrize(
    ("failing_sql", "requests"), [("SELECT missing_column", 1), ("SELECT NULL", 3)]
)
async def test_parse_exhaustion_skips_only_failed_candidate(tmp_path, failing_sql, requests):
    validator = node(tmp_path, ["invalid"] * requests)
    state = get_default_state(
        "test",
        generation_results=[Result(9, "SELECT 1"), Result(2, failing_sql), Result(4, "SELECT 2")],
    )
    final = await validator.run(state)
    assert final["proceed"]
    assert [(r.id, r.rows) for r in final["execution_results"]] == [(9, [(1,)]), (4, [(2,)])]
    assert len(validator.model.requests) == requests


async def test_parse_exhaustion_for_every_candidate_fails_question(tmp_path):
    validator = node(tmp_path, ["invalid"] * 2)
    with pytest.raises(DataAgentError, match="No eligible SQL"):
        await validator.run(
            get_default_state(
                "test", generation_results=[Result(1, "SELECT missing"), Result(2, "SELECT absent")]
            )
        )
    assert len(validator.model.requests) == 2


async def test_result_partial_samples_vote_after_shared_parse_budget(tmp_path):
    validator = node(
        tmp_path,
        ["<result>SELECT 7</result>", "invalid", "invalid", "invalid", "invalid"],
        thinking={"type": "enabled"},
    )
    state = get_default_state("return 7", generation_results=[Result(1, "SELECT NULL")])
    (best,) = (await validator.run(state))["execution_results"]
    assert best.sql == "SELECT 7"
    assert len(validator.model.requests) == 5
    assert all("<reasoning>" not in msgs[0].content for msgs, _ in validator.model.requests)


async def test_unresolved_unsafe_sql_never_reaches_selector(tmp_path):
    unsafe = "DROP TABLE x"
    validator = node(tmp_path, [f"<result>{unsafe}</result>"] * 4)
    with pytest.raises(DataAgentError, match="No eligible SQL"):
        await validator.run(get_default_state("test", generation_results=[Result(1, unsafe)]))


async def test_sqlite_timeout_passes_diagnostic_to_syntax_repair(tmp_path, monkeypatch):
    validator = node(tmp_path, ["<result>SELECT 1</result>"])
    monkeypatch.setattr(SQLiteService, "TIME_OUT", 0)
    expensive_sql = (
        "WITH RECURSIVE nums(n) AS (VALUES (1) UNION ALL SELECT n+1 FROM nums WHERE n<100000) "
        "SELECT SUM(n) FROM nums"
    )
    state = await validator.run(
        get_default_state("return one", generation_results=[Result(0, expensive_sql)])
    )
    assert state["execution_results"][0].rows == [(1,)]
    messages, _ = validator.model.requests[0]
    assert "timed out after 0 seconds" in messages[0].content


async def test_gaussvector_uses_postgres_repair_prompt_and_configured_driver(monkeypatch):
    validator = BirdValidatorNode(
        config=NL2SQLConfig(
            {
                "model": "primary",
                "database": {"engine": "gaussvector", "dialect": "postgres"},
            }
        )
    )
    validator.bind_model(Model(["<result>SELECT 1</result>"]))
    driver = Mock()
    driver.execute.side_effect = [(None, None, "unknown column"), (["value"], [(1,)], None)]
    factory = Mock(return_value=nullcontext(driver))
    monkeypatch.setattr("nl2sql.nodes.bird_validator.build_sql_service", factory)

    state = await validator.run(
        get_default_state("return one", generation_results=[Result(0, "SELECT missing")])
    )
    assert state["execution_results"][0].rows == [(1,)]
    assert all(call.args[0] == "gaussvector" for call in factory.call_args_list)
    [(messages, _)] = validator.model.requests
    assert "executed by PostgreSQL directly" in messages[0].content
    assert "SQLite" not in messages[0].content
