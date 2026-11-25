"""Real SQLite coverage for a standard subagent using host-owned SQL tools."""

import asyncio
import json
import re
import sqlite3
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from dataagent import LaunchOptions, create_agent, prepare_runtime
from dataagent.assembly.graph import load_extensions
from dataagent.data_gateway.sql_security.checker import check_sql
from dataagent.extensions.loading import PythonLoader

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "dataagent/plugins/nl2sql-react"
CONTRACT = {
    "metric": "Total payment",
    "grain": "One total",
    "numerator": "SUM(amount)",
    "denominator": None,
    "joins": [],
    "filters": ["customer_id=6"],
}
SCHEMA_ISSUE = {
    "id": "schema",
    "kind": "database_fact",
    "question": "Which payment tables and keys apply?",
    "acceptance": "Inspect the payment columns and join keys",
}


class SelectingModel(ScriptedModel):
    """Choose an observed ID without hardcoding a program-generated identifier."""

    def _generate(self, messages, *args, **kwargs):
        response = super()._generate(messages, *args, **kwargs)
        message = response.generations[0].message
        latest = next(
            (
                m
                for m in reversed(messages)
                if isinstance(m, ToolMessage)
                and isinstance(m.artifact, dict)
                and "nl2sql_query_control" in m.artifact
            ),
            None,
        )
        control = latest.artifact["nl2sql_query_control"] if latest else None
        if message.content == "REGISTER_SCHEMA":
            message = call("nl2sql-react__update_query_state", {"issues": [SCHEMA_ISSUE]})
        if message.content == "LOCK_CONTRACT":
            resolutions = []
            if control and control["issues"].get("schema", {}).get("status") == "open":
                resolutions = [
                    {
                        "issue_id": "schema",
                        "evidence_ids": [list(control["evidence"])[-1]],
                        "finding": "The payment schema and keys were observed",
                    }
                ]
            message = call(
                "nl2sql-react__update_query_state",
                {"contract": CONTRACT, "resolutions": resolutions},
            )
        if message.content == "SELECT_RESULT":
            observed = next(
                m
                for m in reversed(messages)
                if isinstance(m, ToolMessage) and m.name == "nl2sql-react__execute_sql"
            )
            identifier = re.search(r'"query_id": "([a-f0-9]+)"', observed.text)
            assert identifier, "The result reference must survive native tool offloading"
            message = call(
                "nl2sql-react__finish_query",
                {
                    "execution_id": identifier[1],
                    "conclusion": "117.75",
                    "definitions": ["SUM(amount)"],
                },
            )
        for tool_call in message.tool_calls:
            if tool_call["args"].get("contract_id") == "CURRENT":
                tool_call["args"]["contract_id"] = (
                    control["contract_id"] if control else "not_locked"
                )
        response.generations[0].message = message
        return response


def assert_saved_answer(payload, sql):
    assert payload["status"] == "success" and payload["error"] is None, json.dumps(payload)
    assert payload["sql"] == sql and payload["rows"] == [[117.75]]
    assert payload["returned_row_count"] == 1 and payload["truncated"] is False
    assert payload["query_id"] and payload["artifact_error"] is None
    paths = {Path(p).name: Path(p) for p in payload["artifacts"]}
    assert paths["query.sql"].read_text() == sql
    assert json.loads(paths["data.json"].read_text())["rows"] == [[117.75]]
    assert json.loads(paths["metadata.json"].read_text())["purpose"] == "answer"
    assert (
        json.loads(Path(payload["final_manifest"]).read_text())["query_id"] == payload["query_id"]
    )


@pytest.fixture
def local_db(tmp_path):
    database = tmp_path / "fixture.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE payments(customer_id INTEGER REFERENCES customers(id), amount REAL);
            INSERT INTO customers VALUES(6, 'Customer six');
            INSERT INTO payments VALUES(6, 10.5), (6, 8.25), (6, 99);
        """)
    source = {
        "engine": "sqlite",
        "dialect": "sqlite",
        "db_id": "fixture",
        "connection": {"path": str(database)},
    }
    # Legacy protocol coverage opts out; default-on integration is in test_nl2sql_local_review_gate.
    config = {
        "models": {
            "primary": {
                "name": "unused",
                "base_url": "https://example.invalid/v1",
                "api_key": "unused",
            }
        },
        "plugins": {
            "enabled": ["nl2sql-react"],
            "config": {
                "nl2sql-react": {
                    "database": {"path": str(database), "db_id": "fixture"},
                    "max_rows": 2,
                    "review_sql": False,
                    "deliver_unfinished_answer": False,
                    "control": {"max_probes_per_issue": 2},
                }
            },
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(config))
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))
    loader = PythonLoader()
    run = loader.load(PLUGIN, "nl2sql_local/tools/database.py:_run")
    options = loader.load(PLUGIN, "nl2sql_local/tools/database.py:Config")(
        database={"path": str(database), "db_id": "fixture"}, max_rows=2
    )
    return runtime, source, options, run


def test_metadata_pagination_and_keys(local_db):
    _, source, options, run = local_db
    first = run(
        options,
        source,
        "search_objects",
        {
            "object_type": "table",
            "pattern": "%",
            "detail_level": "names",
            "offset": 0,
            "limit": 1,
        },
        Event(),
    )
    assert first["truncated"] and first["next_offset"] == 1
    full = run(
        options,
        source,
        "search_objects",
        {
            "object_type": "table",
            "pattern": "payments",
            "detail_level": "full",
            "offset": 0,
            "limit": 100,
        },
        Event(),
    )
    assert full["tables"][0]["foreign_keys"][0]["table"] == "customers"
    assert "amount" in full["tables"][0]["columns"]


def test_metadata_filters_only_the_literal_sqlite_prefix(local_db):
    _, source, _, _ = local_db
    from dataagent.data_gateway.sql_service import UnknownTableError, build_sql_service

    with sqlite3.connect(source["connection"]["path"]) as connection:
        connection.executescript(
            "CREATE TABLE sqliteReport(report_value INTEGER);"
            "CREATE TABLE tracked(id INTEGER PRIMARY KEY AUTOINCREMENT);"
        )
    with build_sql_service("sqlite", source["connection"]) as service:
        tables = {item["table_name"] for item in service.search_objects()["objects"]}
        assert "sqliteReport" in tables and "sqlite_sequence" not in tables
        assert service.search_objects("sqliteReport", exact=True)["objects"] == [
            {"table_name": "sqliteReport", "object_type": "table"}
        ]
        columns = service.search_objects("report_value", "column", exact=True)["objects"]
        assert [column["table_name"] for column in columns] == ["sqliteReport"]
        assert "report_value" in service.describe_table("sqliteReport")["columns"]
        with pytest.raises(UnknownTableError):
            service.describe_table("sqlite_sequence")


def test_join_cte_duplicates_empty_and_truncation(local_db):
    _, source, options, run = local_db

    def query(sql):
        return run(options, source, "execute_sql", {"sql": sql}, Event())

    result = query(
        "WITH p AS (SELECT * FROM payments) SELECT SUM(p.amount) "
        "FROM p JOIN customers c ON p.customer_id=c.id WHERE c.id=6"
    )
    assert result["rows"] == [[117.75]] and not result["truncated"]
    assert query("SELECT amount FROM payments")["truncated"]
    assert query("SELECT amount FROM payments LIMIT 2")["truncated"] is False
    assert query("SELECT amount FROM payments WHERE 0")["rows"] == []
    assert query("SELECT 1 UNION ALL SELECT 1")["rows"] == [[1], [1]]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM payments",
        "SELECT 1; DELETE FROM payments",
        "PRAGMA query_only=OFF",
        "ATTACH ':memory:' AS extra",
        "SELECT load_extension('missing')",
    ],
)
def test_write_and_escape_rejected(local_db, sql):
    _, source, options, run = local_db
    with pytest.raises(ValueError):
        run(options, source, "execute_sql", {"sql": sql}, Event())
    with sqlite3.connect(source["connection"]["path"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM payments").fetchone() == (3,)


def test_cancelled_operation_stops(local_db):
    _, source, options, run = local_db
    stop = Event()
    stop.set()
    with pytest.raises(Exception, match="cancelled"):
        run(options, source, "execute_sql", {"sql": "SELECT 1"}, stop)


async def test_native_task_and_tool_errors_without_mcp(local_db):
    runtime, _, _, _ = local_db
    assert not runtime.extensions.mcp_servers
    compiled = load_extensions(runtime)
    child = next(c for c in compiled["subagents"] if c["name"] == "nl2sql-react")
    names = {"nl2sql-react__search_objects", "nl2sql-react__execute_sql"}
    child_names = names | {
        "nl2sql-react__db_explore",
        "nl2sql-react__update_query_state",
        "nl2sql-react__finish_query",
        "nl2sql-react__review_query",
    }
    assert {tool.name for tool in child["tools"]} == child_names
    assert child["filesystem_tools"] == ["read_file"]
    assert not child_names & {
        t.name for t in compiled["tools"]
    }  # SQL execution belongs to the child.
    assert "nl2sql-react__db_explore" not in {t.name for t in compiled["tools"]}
    sql = "SELECT SUM(amount) FROM payments WHERE customer_id=6"
    model = SelectingModel(
        responses=[
            call("task", {"subagent_type": "nl2sql-react", "description": "Total for customer 6"}),
            AIMessage(content="REGISTER_SCHEMA"),
            call(
                "nl2sql-react__search_objects",
                {"pattern": "payments", "detail_level": "full", "issue_id": "schema"},
            ),
            AIMessage(content="LOCK_CONTRACT"),
            call(
                "nl2sql-react__execute_sql",
                {
                    "sql": "SELECT missing FROM payments",
                    "purpose": "answer",
                    "contract_id": "CURRENT",
                },
            ),
            call(
                "nl2sql-react__execute_sql",
                {"sql": sql, "purpose": "answer", "contract_id": "CURRENT"},
            ),
            AIMessage(content="SELECT_RESULT"),
            AIMessage(content="117.75"),
        ]
    )
    async with create_agent(runtime, model=model) as agent:
        result = await agent.ainvoke({"messages": [("user", "Total for customer 6")]})
    reply = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "task")
    assert_saved_answer(json.loads(reply.content), sql)
    observed = [m for request in model.requests for m in request if isinstance(m, ToolMessage)]
    assert any('"error": {' in str(m.content) and "missing" in str(m.content) for m in observed)
    assert any(
        "117.75" in str(m.content) and m.name == "nl2sql-react__execute_sql" for m in observed
    )
    assert not child_names & set(model.tool_schemas[0])
    assert set(model.tool_schemas[1]) == child_names | {"read_file"}
    failed = next(
        json.loads(m.content)
        for m in observed
        if m.name == "nl2sql-react__execute_sql" and json.loads(m.content).get("error")
    )
    assert "query_id" not in failed


@pytest.mark.parametrize("independent,locked", [(False, False), (True, False), (True, True)])
async def test_native_task_explores_then_generates_and_executes(
    local_db, tmp_path, independent, locked
):
    runtime, _, _, _ = local_db
    if independent:
        path = tmp_path / "config.yaml"
        config = json.loads(path.read_text())
        config["plugins"]["config"]["nl2sql-react"]["independent_initial_exploration"] = True
        path.write_text(json.dumps(config))
        runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))
    question = "How much did customer six pay in total?"
    evidence = "customer six refers to customer ID 6; amount is the payment value"
    focus = "Verify the payments-to-customers join and payment grain."
    sql = "SELECT SUM(p.amount) FROM payments p JOIN customers c ON p.customer_id=c.id WHERE c.id=6"
    model = SelectingModel(
        responses=[
            call(
                "task", {"subagent_type": "nl2sql-react", "description": question + "\n" + evidence}
            ),
            *(
                [
                    AIMessage(content="LOCK_CONTRACT"),
                    call(
                        "nl2sql-react__execute_sql",
                        {
                            "sql": "SELECT missing FROM payments",
                            "purpose": "answer",
                            "contract_id": "CURRENT",
                        },
                    ),
                    call(
                        "nl2sql-react__update_query_state",
                        {"issues": [{**SCHEMA_ISSUE, "evidence_ids": ["e1"]}]},
                    ),
                ]
                if locked
                else [AIMessage(content="REGISTER_SCHEMA")]
            ),
            call(
                "nl2sql-react__db_explore",
                {"question": question, "evidence": evidence, "focus": focus, "issue_id": "schema"},
            ),
            call(
                "probe_sql",
                {
                    "sql": "SELECT COUNT(*) AS n, COUNT(DISTINCT customer_id) AS customers FROM payments"
                },
            ),
            call("finish_recall", {"tables": ["payments", "customers"]}),
            AIMessage(content="LOCK_CONTRACT"),
            call(
                "nl2sql-react__execute_sql",
                {"sql": sql, "purpose": "answer", "contract_id": "CURRENT"},
            ),
            AIMessage(content="SELECT_RESULT"),
            AIMessage(content="117.75"),
        ]
    )
    async with create_agent(runtime, model=model) as agent:
        result = await agent.ainvoke({"messages": [("user", question + "\n" + evidence)]})
    reply = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "task")
    assert_saved_answer(json.loads(reply.content), sql)
    # Inspect the actual nested model input and the tool result the ReAct child received.
    inputs = json.loads(model.requests[3 + 2 * int(locked)][1].content)
    if independent and not locked:
        assert inputs["question"] == question + "\n" + evidence
        assert inputs["provided_evidence"] == ""
        assert focus not in inputs["focus"]
        assert "Registered issue" not in inputs["focus"]
        assert "original task independently" in inputs["focus"]
    else:
        assert inputs["question"] == question and inputs["provided_evidence"] == evidence
        assert focus in inputs["focus"]
        assert "Registered issue" in inputs["focus"]
    assert inputs["observations"]
    assert {"search_objects", "describe_table", "probe_sql", "finish_recall"} in [
        set(names) for names in model.tool_schemas
    ]
    messages = [m for r in model.requests for m in r if isinstance(m, ToolMessage)]
    explored = json.loads(next(m.content for m in messages if m.name == "nl2sql-react__db_explore"))
    assert explored["status"] == "ready" and explored["error"] is None
    assert set(explored["schema"]) == {"payments", "customers"}
    assert explored["joins"] == [["payments.customer_id", "customers.id"]]
    assert explored["report"]["model_calls"] == 2
    assert explored["report"]["prefetch_tool_calls"] == 3
    assert explored["report"]["tool_calls"] == 4
    assert explored["report"]["observations"][-1]["result"]["rows"] == [[3, 1]]
    assert not explored["report"]["unresolved"]


async def test_native_large_result_is_saved_before_offloading(local_db, tmp_path):
    runtime, source, _, _ = local_db
    with sqlite3.connect(source["connection"]["path"]) as connection:
        connection.execute("UPDATE customers SET name=?", ("x" * 2000,))
        connection.executemany(
            "INSERT INTO customers VALUES(?, ?)", [(i, "x" * 2000) for i in range(100, 200)]
        )
    config_path = tmp_path / "config.yaml"
    config = json.loads(config_path.read_text())
    config["plugins"]["config"]["nl2sql-react"]["max_rows"] = 200
    config_path.write_text(json.dumps(config))
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=config_path))

    model = SelectingModel(
        responses=[
            call("task", {"subagent_type": "nl2sql-react", "description": "List customer names"}),
            AIMessage(content="LOCK_CONTRACT"),
            call(
                "nl2sql-react__execute_sql",
                {
                    "sql": "SELECT name FROM customers",
                    "purpose": "answer",
                    "contract_id": "CURRENT",
                },
            ),
            AIMessage(content="SELECT_RESULT"),
            AIMessage(content="Saved"),
        ]
    )
    async with create_agent(runtime, model=model) as agent:
        state = await agent.ainvoke({"messages": [("user", "List customer names")]})
    # The parent task reply can itself be offloaded; its full final manifest is on disk.
    observed = next(
        m
        for m in model.requests[3]
        if isinstance(m, ToolMessage) and m.name == "nl2sql-react__execute_sql"
    )
    assert "offloaded" in observed.text.lower() or "too large" in observed.text.lower()
    record = observed.artifact["nl2sql_local_result"]
    saved = next(Path(path) for path in record["artifacts"] if path.endswith("data.json"))
    assert json.loads(saved.read_text())["rows"] == [["x" * 2000]] * 101
    manifest = saved.parents[2] / "final.json"
    assert json.loads(manifest.read_text())["query_id"] == record["query_id"]
    assert state["messages"][-1].content == "Saved"


async def test_native_sequential_local_tasks_keep_separate_results(local_db):
    runtime, _, _, _ = local_db

    class SequentialModel(ScriptedModel):
        def _generate(self, messages, *args, **kwargs):
            self.requests.append(messages)
            last = messages[-1]
            if isinstance(last, ToolMessage) and last.name == "task":
                replies = [m for m in messages if isinstance(m, ToolMessage) and m.name == "task"]
                if len(replies) == 1:
                    response = call(
                        "task",
                        {
                            "subagent_type": "nl2sql-react",
                            "description": "Recheck the total for customer 6 as a decimal",
                        },
                    )
                else:
                    response = AIMessage(content="Done")
            elif isinstance(last, ToolMessage) and last.name == "nl2sql-react__execute_sql":
                observed = json.loads(last.content)
                response = call(
                    "nl2sql-react__finish_query", {"execution_id": observed["query_id"]}
                )
            elif isinstance(last, ToolMessage):
                response = call(
                    "nl2sql-react__execute_sql",
                    {
                        "sql": "SELECT SUM(amount) FROM payments WHERE customer_id=6",
                        "purpose": "answer",
                        "contract_id": json.loads(last.content)["contract_id"],
                    },
                )
            elif last.content == "Delegate twice":
                response = call(
                    "task", {"subagent_type": "nl2sql-react", "description": "Total for customer 6"}
                )
            else:
                response = call("nl2sql-react__update_query_state", {"contract": CONTRACT})
            return ChatResult(generations=[ChatGeneration(message=response)])

    model = SequentialModel(responses=[])
    async with create_agent(runtime, model=model) as agent:
        state = await agent.ainvoke({"messages": [("user", "Delegate twice")]})
    replies = [
        json.loads(m.content)
        for m in state["messages"]
        if isinstance(m, ToolMessage) and m.name == "task"
    ]
    assert len(replies) == 2 and all(result["status"] == "success" for result in replies)
    assert replies[0]["task_id"] != replies[1]["task_id"]
    assert replies[0]["query_id"] != replies[1]["query_id"]
    assert all(Path(result["final_manifest"]).is_file() for result in replies)


def exploration_tool(source, model, **options):
    """Exercise the real tool adapter and real recall loop with task-local limits."""
    tool = PythonLoader().load(PLUGIN, "nl2sql_local/tools/database.py:db_explore")

    async def invoke(*args, **kwargs):
        return (await tool.coroutine(*args, **kwargs))[0]

    context = SimpleNamespace(
        get_plugin_config=lambda _: {
            "database": {**source["connection"], "db_id": source.get("db_id")},
            "dbexplore": options,
        },
        get_model=lambda: model,
        redaction_secrets=("test-secret",),
    )
    return invoke, SimpleNamespace(context=context)


async def test_exploration_unresolved_does_not_claim_ready(local_db):
    _, source, _, _ = local_db
    model = ScriptedModel(
        responses=[
            call(
                "finish_recall",
                {
                    "tables": ["payments"],
                    "unresolved": ["Does net payment exclude refunds?"],
                },
            )
        ]
    )
    invoke, runtime = exploration_tool(source, model)
    result = await invoke("Net payments?", runtime)
    assert result["status"] == "unresolved" and "payments" in result["schema"]
    assert result["error"]["code"] == "LIMIT_EXCEEDED"
    assert result["report"]["unresolved"] == ["Does net payment exclude refunds?"]
    assert result["report"]["observations"]


async def test_react_exploration_forces_submission_after_one_tool_batch(local_db):
    _, source, _, _ = local_db
    model = ScriptedModel(
        responses=[
            call("probe_sql", {"sql": "SELECT COUNT(*) FROM payments"}),
            call("finish_recall", {"tables": ["payments"]}),
        ]
    )
    invoke, runtime = exploration_tool(source, model)
    result = await invoke("Total payments?", runtime)
    assert result["status"] == "ready" and result["report"]["tool_rounds"] == 1
    assert set(model.tool_schemas[0]) == {
        "search_objects",
        "describe_table",
        "probe_sql",
        "finish_recall",
    }
    assert model.tool_schemas[1] == ["finish_recall"]
    from dataagent.db_explore import RecallOptions

    assert RecallOptions().max_tool_rounds is None  # Original workflow default is unchanged.


@pytest.mark.parametrize(
    "table_count,options,rounds",
    [(8, {}, 1), (9, {}, 2), (9, {"max_tool_rounds": 1}, 1)],
    ids=["eight-tables", "nine-tables-default", "nine-tables-explicit-limit"],
)
async def test_catalog_leaves_a_probe_round_after_describing(
    local_db, table_count, options, rounds
):
    _, source, _, _ = local_db
    with sqlite3.connect(source["connection"]["path"]) as connection:
        for index in range(table_count - 2):
            connection.execute(f"CREATE TABLE extra_{index}(id INTEGER)")
    large = table_count > 8
    probe_allowed = not large or rounds == 2
    calls = [call("describe_table", {"table": "payments"})] if large else []
    if probe_allowed:
        calls.append(call("probe_sql", {"sql": "SELECT COUNT(*) AS n FROM payments"}))
    calls.append(call("finish_recall", {"tables": ["payments"]}))
    model = ScriptedModel(responses=calls)
    invoke, runtime = exploration_tool(source, model, **options)

    result = await invoke("Check payment coverage", runtime)

    assert result["status"] == "ready"
    assert result["report"]["prefetch_tool_calls"] == (1 if large else table_count + 1)
    assert result["report"]["effective_max_tool_rounds"] == rounds
    assert result["report"]["tool_rounds"] == rounds
    probe_round = 1 if large else 0
    assert ("probe_sql" in model.tool_schemas[probe_round]) is probe_allowed
    if probe_allowed:
        probe = next(o for o in result["report"]["observations"] if o["tool"] == "probe_sql")
        assert probe["result"]["rows"] == [[3]]
    assert model.tool_schemas[-1] == ["finish_recall"]


async def test_exploration_probe_preserves_named_cte_columns(local_db):
    _, source, _, _ = local_db
    model = ScriptedModel(
        responses=[
            call(
                "probe_sql",
                {
                    "sql": 'WITH p("total value") AS (SELECT SUM(amount) FROM payments) '
                    'SELECT p."total value" FROM p'
                },
            ),
            call("finish_recall", {"tables": ["payments"]}),
        ]
    )
    invoke, runtime = exploration_tool(source, model)

    result = await invoke("Check payment totals", runtime)

    assert result["status"] == "ready"
    probe = next(o for o in result["report"]["observations"] if o["tool"] == "probe_sql")
    assert "error" not in probe["result"], probe["result"]
    assert probe["result"]["columns"] == ["total value"]
    assert probe["result"]["rows"] == [[117.75]]


def test_named_cte_does_not_exempt_same_name_function_from_allowlist():
    result = check_sql("WITH p(n) AS (SELECT 1) SELECT p(n) FROM p", dialect="sqlite", schema={})

    assert [(v.rule_id, v.message) for v in result.violations] == [
        ("FUNCTION-001", "SQL function is not in the allowlist: p.")
    ]


async def test_exploration_budget_and_timeout_are_reported(local_db):
    _, source, _, _ = local_db
    model = ScriptedModel(responses=[AIMessage(content="thinking"), AIMessage(content="thinking")])
    invoke, runtime = exploration_tool(source, model, max_steps=2)
    result = await invoke("Find payment context", runtime)
    assert result["status"] == "error" and result["report"]["stop_reason"] == "budget"
    assert result["report"]["model_calls"] == 2
    assert result["limits"]["max_steps"] == 2

    class SlowModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            await asyncio.sleep(10)

    invoke, runtime = exploration_tool(source, SlowModel(responses=[]), timeout_seconds=0.05)
    result = await invoke("Find payment context", runtime)
    assert result["status"] == "error" and result["report"]["stop_reason"] == "timeout"
    assert result["report"]["elapsed_ms"] >= 0


async def test_exploration_cancellation_is_not_converted_to_a_tool_error(local_db):
    _, source, _, _ = local_db
    entered = asyncio.Event()

    class WaitingModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    invoke, runtime = exploration_tool(source, WaitingModel(responses=[]))
    task = asyncio.create_task(invoke("Find payment context", runtime))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_exploration_model_error_is_redacted(local_db):
    _, source, _, _ = local_db

    class BrokenModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            raise RuntimeError("provider failed: test-secret")

    invoke, runtime = exploration_tool(source, BrokenModel(responses=[]))
    result = await invoke("Find payment context", runtime)
    assert result["status"] == "error"
    assert "test-secret" not in json.dumps(result) and "[REDACTED]" in result["error"]["message"]


async def test_exploration_adapter_preserves_configured_column_descriptions(local_db, tmp_path):
    _, source, _, _ = local_db
    directory = tmp_path / "column-docs"
    directory.mkdir()
    (directory / "payments.csv").write_text(
        "original_column_name,column_description,value_description\namount,Payment in cents,USD cents\n"
    )
    source = {**source, "connection": {**source["connection"], "descriptions_dir": str(directory)}}
    model = ScriptedModel(responses=[call("finish_recall", {"tables": ["payments"]})])
    invoke, runtime = exploration_tool(source, model)
    result = await invoke("Total payments?", runtime)
    assert result["status"] == "ready"
    assert "Payment in cents" in result["schema"]["payments"]["columns"]["amount"]["description"]
