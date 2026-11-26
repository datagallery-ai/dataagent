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
"""Tests for the nl2sql builtin plugin (compiled sub-agent).

Runs the full host pipeline without network: ``select_plugins`` +
``compile_extensions`` with plugin parameters and a stub model resolver,
``create_deep_agent`` with a scripted parent model that delegates through the
native ``task`` tool, then assertions on the JSON reply the compiled graph
returns. The plugin's business-parameter validation is covered separately.
"""

import asyncio
import csv
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import mcp
import pytest
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from mcp.client import streamable_http

from dataagent import DataAgentError, build_sql_service
from dataagent.bootstrap import LaunchOptions, prepare_runtime
from dataagent.client import create_agent
from dataagent.config.settings import Settings
from dataagent.core.graph import create_deep_agent
from dataagent.extensions import compile_extensions, select_plugins
from dataagent.semantic_client import SemanticServiceClient

PROJECT = Path(__file__).resolve().parents[1]
BUILTIN_ROOT = PROJECT / "dataagent/plugins"
PLUGIN_ROOT = BUILTIN_ROOT / "nl2sql"

# Direct ``nl2sql`` imports in the unit tests below need the plugin root
# importable; the compiled-subagent pipeline needs no such global entry.
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

EXPECTED_SQL = (
    "SELECT department, COUNT(*) AS cnt FROM employees GROUP BY department ORDER BY cnt DESC"
)

_PLUGIN_RESPONSES = [
    AIMessage(content=f"```sql\n{EXPECTED_SQL}\n```"),  # generator
    AIMessage(
        content='```json\n[{"id": 0, "score": 1, "issues": []}]\n```'
    ),  # validator: validate_semantic
    AIMessage(content='```json\n[{"score": 1, "issues": []}]\n```'),  # selector
]


@pytest.mark.parametrize(
    "engine,driver,requirement",
    [
        ("mysql", "pymysql", "datagallery-dataagent"),
        ("gaussvector", "psycopg2", "datagallery-dataagent"),
        ("hive", "pyspark", "datagallery-dataagent[spark]"),
        ("spark", "pyspark", "datagallery-dataagent[spark]"),
    ],
)
def test_missing_driver_identifies_required_installation(monkeypatch, engine, driver, requirement):
    monkeypatch.setitem(sys.modules, driver, None)
    params = {"model": "primary", "database": {"dialect": engine, "engine": engine}}
    model = ScriptedModel(responses=[])
    with pytest.raises(DataAgentError) as error:
        _compile(params, model)
    assert error.value.fact.endswith(f"请在运行环境安装 {requirement}")
    assert error.value.source == "config" and error.value.component == "nl2sql"
    assert ("可选依赖" in error.value.fact) == (engine in {"hive", "spark"})
    # SQL-only assembly needs no database driver. EXPLAIN alone needs one.
    params["nodes"] = ["perceptor", "generator"]
    assert _compile(params, model)["subagents"][0]["name"] == "nl2sql"
    params["nodes"].append("validator")
    params["validator"] = {"db_explain": True}
    with pytest.raises(DataAgentError, match="请在运行环境安装"):
        _compile(params, model)


async def test_task_end_to_end_on_sqlite(tmp_path):
    payload, plugin_model = await _run_task(tmp_path)

    assert payload["sql"] == EXPECTED_SQL
    assert payload["confidence"] == 1.0
    assert payload["columns"] == ["department", "cnt"]
    assert payload["row_count"] == 6  # six departments, limit -1 keeps all
    assert payload["total_known"] is True  # unbounded fetch → the count is the full result
    assert payload["truncated"] is False
    assert payload["issues"] == []
    assert payload["error"] is None
    assert payload["rows_preview"][0] == ["Engineering", 2]  # ordered by cnt DESC
    # preview_limit 5 shows five of six rows; the reply says so instead of leaving it implicit
    assert len(payload["rows_preview"]) == 5
    assert "has 6 rows" in payload["rows_note"] and "first 5" in payload["rows_note"]
    assert "result_file" not in payload  # no session output directory → no snapshot

    # the compiled graph made exactly its three scripted model calls, each
    # opening with the node's system prompt
    assert len(plugin_model.requests) == 3
    assert all(isinstance(request[0], SystemMessage) for request in plugin_model.requests)
    assert all(isinstance(request[1], HumanMessage) for request in plugin_model.requests)


async def test_sql_security_check_runs_in_the_compiled_plugin(tmp_path):
    # The plugin loader drops plain ``nl2sql`` module names once assembly
    # finishes, so the security check must not import anything lazily at run
    # time: it used to fail the whole task with "No module named 'nl2sql'".
    # Reproduce the server's import environment: no plugin root on sys.path
    # and no plain-name nl2sql modules left in sys.modules.
    secured_sql = (
        "SELECT department, COUNT(*) AS cnt FROM employees "
        "WHERE salary > 0 GROUP BY department ORDER BY cnt DESC"
    )

    def handler(request):
        if request.url.path.endswith("/advanced-search/table-list"):
            return httpx.Response(200, json=[{"staff.employees": {"table_description": "员工表"}}])
        if request.url.path.endswith("/advanced-search/table-columns-info"):
            return httpx.Response(
                200,
                json={
                    "staff.employees.id": {
                        "column_short_description": "ID",
                        "value_type": "INTEGER",
                        "value_description": "1",
                    },
                    "staff.employees.name": {
                        "column_short_description": "姓名",
                        "value_type": "TEXT",
                        "value_description": "Alice",
                    },
                    "staff.employees.department": {
                        "column_short_description": "部门",
                        "value_type": "TEXT",
                        "value_description": "Engineering",
                    },
                    "staff.employees.salary": {
                        "column_short_description": "薪资",
                        "value_type": "REAL",
                        "value_description": "120.0",
                    },
                },
            )
        return httpx.Response(200, json=[])

    client = SemanticServiceClient(
        "http://semantic.internal", transport=httpx.MockTransport(handler)
    )
    saved_path = list(sys.path)
    saved_modules = {
        key: module
        for key, module in sys.modules.items()
        if key == "nl2sql" or key.startswith("nl2sql.")
    }
    sys.path[:] = [entry for entry in sys.path if entry != str(PLUGIN_ROOT)]
    for key in saved_modules:
        sys.modules.pop(key, None)
    try:
        with client:
            payload, _ = await _run_task(
                tmp_path,
                params={
                    "perceptor": {"schema_mode": "full_schema"},  # schema dict from the service
                    "validator": {"sql_security_enabled": True, "db_explain": True},
                },
                plugin_responses=[
                    AIMessage(content=f"```sql\n{secured_sql}\n```"),  # generator
                    AIMessage(content='```json\n[{"id": 0, "score": 1, "issues": []}]\n```'),
                    AIMessage(content='```json\n[{"score": 1, "issues": []}]\n```'),
                ],
                semantic_client=client,
            )
    finally:
        sys.path[:] = saved_path
        sys.modules.update(saved_modules)

    assert payload["sql"] == secured_sql
    assert payload["error"] is None


@pytest.mark.parametrize("security_enabled", [None, False, True], ids=["default", "off", "on"])
async def test_sql_security_switch_in_delegated_workflow(tmp_path, security_enabled):
    sql = "SELECT salary FROM employees"
    validator = {"semantic_check": False}
    if security_enabled is not None:
        validator["sql_security_enabled"] = security_enabled
    payload, _ = await _run_task(
        tmp_path,
        params={
            "validator": validator,
            "reflector": {"ref_retries": 0},
            "selector": {"shortcut": 1},
        },
        plugin_responses=[AIMessage(content=f"```sql\n{sql}\n```")],
    )

    if security_enabled:
        assert payload["source"] == "constraint"
        assert "RESOURCE-009" in payload["error"]
    else:
        assert payload["sql"] == sql
        assert payload["row_count"] == 7


async def test_native_stream_reports_nl2sql_steps_while_model_is_waiting(tmp_path):
    from ag_ui.core import RunAgentInput, UserMessage
    from ag_ui_langgraph import LangGraphAgent
    from langgraph.checkpoint.memory import InMemorySaver

    release = asyncio.Event()
    generating = asyncio.Event()

    class WaitingModel(ScriptedModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            await release.wait()
            return await super()._agenerate(messages, stop, run_manager, **kwargs)

    db, schema = _sqlite_fixture(tmp_path)
    graph = _build_agent(
        _plugin_params(db, schema),
        ScriptedModel(
            responses=[
                call(
                    "task", {"subagent_type": "nl2sql", "description": "count staff"}, id="delegate"
                ),
                AIMessage(content="root answer"),
            ]
        ),
        WaitingModel(responses=list(_PLUGIN_RESPONSES)),
    )
    graph.checkpointer = InMemorySaver()
    adapter = LangGraphAgent(
        name="test", graph=graph, emit_raw_events=False, subagent_visibility="attributed"
    )
    body = RunAgentInput(
        thread_id="thread",
        run_id="root",
        messages=[UserMessage(id="u", content="count staff")],
        tools=[],
        context=[],
        state={},
        forwarded_props={},
    )
    updates = []

    async def consume():
        async for event in adapter.run(body):
            if event is not None and getattr(event, "subagent_run_id", None):
                updates.append(event.model_dump(mode="json", by_alias=True, exclude_none=True))
                if event.type == "STEP_STARTED" and event.step_name == "generator":
                    generating.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(generating.wait(), 5)
        assert not task.done()
        child = next(event for event in updates if event["type"] == "SUBAGENT_STARTED")
        assert child["parentToolCallId"] == "delegate"
        assert updates[-1]["subagentRunId"] == child["subagentRunId"]
        release.set()
        await asyncio.wait_for(task, 5)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert [event["stepName"] for event in updates if event["type"] == "STEP_STARTED"] == [
        "perceptor",
        "generator",
        "validator",
        "reflector",
        "executor",
        "selector",
    ]
    assert updates[-1]["type"] == "SUBAGENT_FINISHED"


async def test_task_reports_truncation_with_bounded_fetch(tmp_path):
    payload, _ = await _run_task(tmp_path, params={"executor": {"limit": 3, "preview_limit": 5}})

    # the engine fetched at most limit+1 rows; one extra row flags truncation
    assert payload["row_count"] == 3
    assert payload["total_known"] is False
    assert payload["truncated"] is True
    assert len(payload["rows_preview"]) == 3
    assert "exceeds the fetch limit" in payload["rows_note"]


async def test_bounded_fetch_below_the_limit_still_knows_its_total(tmp_path):
    payload, _ = await _run_task(tmp_path, params={"executor": {"limit": 10, "preview_limit": 5}})

    # a bounded fetch that stayed under the limit holds the complete result
    assert payload["row_count"] == 6
    assert payload["total_known"] is True
    assert payload["truncated"] is False


async def test_preview_covering_the_whole_result_has_no_rows_note(tmp_path):
    payload, _ = await _run_task(tmp_path, params={"executor": {"limit": -1, "preview_limit": 10}})

    assert len(payload["rows_preview"]) == payload["row_count"] == 6
    assert "rows_note" not in payload


async def test_executed_query_writes_a_session_result_file(tmp_path):
    outputs = tmp_path / "outputs"
    payload, _ = await _run_task(tmp_path, outputs_dir=outputs)

    result_file = Path(payload["result_file"])
    assert result_file.parent == outputs / "nl2sql"
    assert result_file.name == f"{payload['query_id']}.csv"
    with result_file.open(encoding="utf-8", newline="") as file:
        header, *rows = list(csv.reader(file))
    # every fetched row with raw values, not just the five-row preview
    assert header == ["department", "cnt"]
    assert len(rows) == 6 and rows[0] == ["Engineering", "2"]
    assert (
        "result_file" in payload["rows_note"]
        and "do not dispatch the question again" in payload["rows_note"]
    )

    meta = json.loads(
        result_file.with_name(f"{payload['query_id']}.meta.json").read_text(encoding="utf-8")
    )
    assert meta["question"] == "每个部门的员工人数"
    assert meta["sql"] == EXPECTED_SQL
    assert (meta["db_id"], meta["dialect"], meta["engine"]) == ("staff", "sqlite", "sqlite")
    assert (meta["row_count"], meta["total_known"], meta["truncated"]) == (6, True, False)
    assert "staff.sqlite" not in json.dumps(meta)  # no connection parameters


async def test_result_files_can_be_disabled(tmp_path):
    outputs = tmp_path / "outputs"
    payload, _ = await _run_task(tmp_path, params={"result_files": False}, outputs_dir=outputs)

    assert "result_file" not in payload and "query_id" not in payload
    assert not (outputs / "nl2sql").exists()


async def test_failed_execution_writes_no_result_file(tmp_path):
    outputs = tmp_path / "outputs"
    payload, _ = await _run_task(
        tmp_path,
        outputs_dir=outputs,
        plugin_responses=[
            AIMessage(content="```sql\nSELECT missing_column FROM employees\n```"),
            AIMessage(content='```json\n[{"id": 0, "score": 1, "issues": []}]\n```'),
            AIMessage(content='```json\n[{"score": 1, "issues": []}]\n```'),
        ],
        params={"validator": {"db_explain": False}},
    )

    assert payload["error"]
    assert "result_file" not in payload
    assert not (outputs / "nl2sql").exists()


async def test_factory_error_preserves_the_structured_task_reply(tmp_path):
    payload, _ = await _run_task(
        tmp_path,
        params={
            "database": {"dialect": "sqlite", "engine": "unsupported"},
            "validator": {"db_explain": False},
        },
    )
    assert payload == {
        "error": "Unsupported database engine: unsupported",
        "source": "tool",
        "component": "nl2sql",
    }


async def test_model_call_budget_surfaces_as_error_reply(tmp_path):
    payload, plugin_model = await _run_task(
        tmp_path, params={"model_call_budget": 1}, plugin_responses=_PLUGIN_RESPONSES[:1]
    )

    # the generator consumed the only budgeted call; the validator's call is refused
    assert payload["error"]
    assert payload["source"] == "llm"
    assert len(plugin_model.requests) == 1


async def test_task_builds_schema_from_host_semantic_client(tmp_path):
    requests_seen = []

    def handler(request):
        requests_seen.append(request.url.path)
        if request.url.path.endswith("/advanced-search/table-list"):
            return httpx.Response(200, json=[{"staff.employees": {"table_description": "员工表"}}])
        if request.url.path.endswith("/advanced-search/table-columns-info"):
            return httpx.Response(
                200,
                json={
                    "staff.employees.department": {
                        "column_short_description": "部门",
                        "value_type": "TEXT",
                        "value_description": "Engineering",
                    },
                    "staff.employees.salary": {
                        "column_short_description": "薪资",
                        "value_type": "REAL",
                        "value_description": "120.0",
                    },
                },
            )
        return httpx.Response(200, json=[])

    client = SemanticServiceClient(
        "http://semantic.internal", transport=httpx.MockTransport(handler)
    )
    with client:
        payload, _ = await _run_task(
            tmp_path,
            params={
                "perceptor": {
                    "schema_mode": "full_schema"
                },  # no user_schema: schema from the service
            },
            semantic_client=client,
        )

    assert payload["sql"] == EXPECTED_SQL
    assert payload["row_count"] == 6
    assert [path.rsplit("/", 1)[-1] for path in requests_seen] == [
        "table-list",
        "table-columns-info",
        "joinable-tables",
    ]


async def test_missing_host_semantic_client_is_a_config_error(tmp_path):
    """Without a host client the perceptor fails fast instead of using plugin config."""
    payload, _ = await _run_task(
        tmp_path,
        params={
            "perceptor": {"schema_mode": "full_schema"},  # no user_schema, no client
        },
    )

    assert payload["source"] == "config"
    assert "semantic 服务未配置" in payload["error"]


@pytest.mark.parametrize("execute", [False, True])
def test_parent_agent_sees_bound_database_and_routing_without_connection_keys(tmp_path, execute):
    """The parent sees the effective execution mode and routing, without connection secrets."""
    db_path, schema_path = _sqlite_fixture(tmp_path)
    params = _plugin_params(db_path, schema_path)
    if not execute:
        params.update(nodes=["perceptor", "generator"])
    extensions = _compile(params, ScriptedModel(responses=[]))

    (child,) = extensions["subagents"]
    assert "db_id=staff, dialect=sqlite, engine=sqlite" in child["description"]
    assert str(db_path) not in child["description"]
    # how to use the subagent lives in its description; the root prompt only
    # restricts the parent's other tools, scoped to the configured database
    assert "result_file" in child["description"] and "one SQL query" in child["description"]
    description = child.get("description")
    assert "It does not analyze, explain or summarize the data" in description
    mode = "query and execute" if execute else "generate SQL only"
    assert f"**Current mode: {mode}**" in description
    other_mode = "generate SQL only" if execute else "query and execute"
    assert f"**Current mode: {other_mode}**" not in description
    assert "SQL has not been executed" in description
    assert "do not translate them" in child["description"]
    assert "only way to query the configured database" in extensions["system_prompt"]
    assert "write_todos" not in extensions["system_prompt"]
    assert "rows_note" not in extensions["system_prompt"]


def test_unknown_configuration_sections_are_rejected():
    from nl2sql.config_manager import NL2SQLConfig

    with pytest.raises(ValueError, match="unknown nl2sql parameter"):
        NL2SQLConfig(
            {"MODEL": {}, "CORE": {}, "model": "primary", "database": {"dialect": "sqlite"}}
        )


def test_result_files_switch_must_be_boolean():
    from nl2sql.config_manager import NL2SQLConfig

    with pytest.raises(ValueError, match="result_files"):
        NL2SQLConfig({"model": "primary", "database": {"dialect": "sqlite"}, "result_files": "yes"})


def test_node_sections_default_to_full_chain_and_require_prefix():
    from nl2sql.config_manager import NL2SQLConfig

    base = {"model": "primary", "database": {"dialect": "sqlite", "path": "/tmp/x.db"}}
    full_chain = ["perceptor", "generator", "validator", "reflector", "executor", "selector"]
    assert NL2SQLConfig(base).enabled_nodes == full_chain
    assert NL2SQLConfig({**base, "perceptor": {}, "generator": {}}).enabled_nodes == [
        "perceptor",
        "generator",
    ]
    # the node-chain checks are lazy: they fire when the graph is assembled
    with pytest.raises(ValueError, match="prefix"):
        NL2SQLConfig({**base, "generator": {}, "executor": {}}).enabled_nodes
    with pytest.raises(ValueError, match="generator"):
        NL2SQLConfig({**base, "perceptor": {}}).enabled_nodes


@pytest.mark.parametrize("custom_output", [False, True])
async def test_compiled_entry_runs_nl2sql_and_writes_the_session_file(tmp_path, custom_output):
    db_path, schema_path = _sqlite_fixture(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": "test-model",
                        "base_url": "https://example.invalid/v1",
                        "api_key": "test-secret",
                    }
                },
                "dataagent": {
                    "entry": "nl2sql",
                    "output_root": str(tmp_path / "reports") if custom_output else None,
                },
                "plugins": {
                    "enabled": ["nl2sql"],
                    "config": {"nl2sql": _plugin_params(db_path, schema_path)},
                },
            }
        )
    )
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=config))
    result = await create_agent(
        runtime, model=ScriptedModel(responses=list(_PLUGIN_RESPONSES))
    ).ainvoke(
        {"messages": [HumanMessage(content="每个部门的员工人数")]},
    )
    message = result["messages"][-1]
    assert isinstance(message, AIMessage)
    payload = json.loads(message.content)
    assert payload["query_id"] and payload["error"] is None
    result_file = Path(payload["result_file"])
    assert result_file.is_file()
    assert result_file.parent == runtime.paths.for_session("sdk").outputs / "nl2sql"
    session = runtime.paths.for_session("sdk")
    from dataagent.bootstrap.paths import read_session_binding

    assert read_session_binding(session).outputs == session.outputs
    if custom_output:
        assert session.outputs == tmp_path / "reports/default/sdk"
    assert result_file.name == f"{payload['query_id']}.csv"


@pytest.mark.parametrize("entry", [True, False])
@pytest.mark.parametrize("transport", ["sdk", "http"])
@pytest.mark.parametrize("stage", ["generator", "validator", "selector"])
async def test_compiled_nl2sql_propagates_loop_detection(
    tmp_path, monkeypatch, entry, transport, stage
):
    """NL2SQL wraps node errors into a reply; a detected loop must still stop the run."""
    from test_api import client_for, query, sse, terminal

    from dataagent import LoopDetectedError

    db_path, schema_path = _sqlite_fixture(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": "test-model",
                        "base_url": "https://example.invalid/v1",
                        "api_key": "test-secret",
                    }
                },
                "dataagent": {"entry": "nl2sql" if entry else None},
                "plugins": {
                    "enabled": ["nl2sql"],
                    "config": {"nl2sql": _plugin_params(db_path, schema_path)},
                },
            }
        )
    )
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=config))
    statements = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    repeated = AIMessage(content="We need to analyze the same question again. " * 60)
    stage_index = ["generator", "validator", "selector"].index(stage)
    responses = [*_PLUGIN_RESPONSES[:stage_index], repeated]
    if not entry:
        responses.insert(
            0,
            call(
                "task",
                {
                    "subagent_type": "nl2sql",
                    "description": "每个部门的员工人数",
                },
            ),
        )
    # Valid replies after the loop expose retries and downstream SQL/CSV side effects.
    model = ScriptedModel(
        responses=[
            *responses,
            *_PLUGIN_RESPONSES[stage_index:],
            AIMessage(content="must not continue"),
        ]
    )
    if transport == "sdk":
        async with create_agent(runtime, model=model) as agent:
            with pytest.raises(LoopDetectedError):
                await agent.ainvoke({"messages": [HumanMessage(content="每个部门的员工人数")]})
    else:
        async with client_for(runtime, model) as (client, _):
            events = sse(await client.post("/api/v1/runs", json=query("每个部门的员工人数")))
        assert terminal(events) == ["RUN_ERROR"]
        assert events[-1]["code"] == "LOOP_DETECTED"
    assert len(model.requests) == len(responses)
    assert statements.count(EXPECTED_SQL) == (1 if stage == "selector" else 0)
    assert not list(runtime.paths.output_root.rglob("*.csv"))


@pytest.fixture(params=["node", "scenario"])
def json_model_call(request, tmp_path):
    """Exercise both JSON entrypoints with their real model and prompt plumbing."""
    if request.param == "node":
        from nl2sql.config_manager import NL2SQLConfig
        from nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode

        node = BaseNL2SQLNode(
            "selector",
            NL2SQLConfig(
                {
                    "model": "primary",
                    "database": {"dialect": "sqlite"},
                }
            ),
        )

        async def invoke(model):
            node.bind_model(model)
            return await node.execute_with_llm_json(
                {
                    "schema": "",
                    "question": "test",
                    "sql_rules": "",
                    "res": "[]",
                }
            )
    else:
        from scenarios.udn.common.llm import execute_with_llm_json

        (tmp_path / "system.md").write_text("Return JSON.")
        (tmp_path / "user.md").write_text("{{ question }}")

        async def invoke(model):
            return await execute_with_llm_json(model, tmp_path, "", {"question": "test"})

    return invoke


async def test_json_model_calls_do_not_retry_loop_errors(json_model_call):
    from dataagent import LoopDetectedError
    from dataagent.extensions.loop_detection import LoopDetectionCallback

    model = ScriptedModel(
        responses=[
            AIMessage(content="We need to analyze the same question again. " * 60),
            AIMessage(content='```json\n{"ok": true}\n```'),
        ],
        callbacks=[LoopDetectionCallback()],
    )
    with pytest.raises(LoopDetectedError):
        await json_model_call(model)
    assert len(model.requests) == 1


@pytest.mark.parametrize("recovers", [False, True])
async def test_json_model_calls_keep_format_retries(json_model_call, recovers):
    from dataagent import DataAgentError

    model = ScriptedModel(
        responses=[
            AIMessage(content="No JSON fence"),
            AIMessage(content="```json\n{broken\n```"),
            AIMessage(
                content='```json\n{"ok": true}\n```' if recovers else "```json\n{broken\n```"
            ),
        ]
    )
    if recovers:
        assert await json_model_call(model) == {"ok": True}
    else:
        with pytest.raises(DataAgentError, match="JSON parsing failed after 3 attempts"):
            await json_model_call(model)
    assert len(model.requests) == 3


def test_connection_config_strips_reserved_database_keys():
    from nl2sql.config_manager import NL2SQLConfig

    config = NL2SQLConfig(
        {
            "model": "primary",
            "database": {
                "db_id": "staff",
                "dialect": "sqlite",
                "engine": "sqlite",
                "path": "/tmp/staff.db",
            },
        }
    )
    assert config.connection_config() == {"path": "/tmp/staff.db"}


MCP_DATABASE = {"engine": "mcp", "dialect": "mysql", "path": "http://mcp.invalid/mcp"}


@pytest.mark.parametrize(
    ("overrides", "remove", "message"),
    [
        ({}, ("path",), "path"),
        ({"unknown": True}, (), "unknown"),
        ({"path": 42}, (), "database.path"),
        ({"read_tool": None}, (), "database.read_tool"),
        ({"timeout": "30"}, (), "database.timeout"),
        ({"timeout": True}, (), "database.timeout"),
        ({"explain_prefix": []}, (), "database.explain_prefix"),
    ],
)
def test_mcp_database_config_rejects_invalid_parameters(overrides, remove, message):
    from nl2sql.config_manager import NL2SQLConfig

    database = {key: value for key, value in MCP_DATABASE.items() if key not in remove}
    database.update(overrides)
    with pytest.raises(ValueError, match=message):
        NL2SQLConfig({"model": "primary", "database": database})


@pytest.fixture
def mcp_backend(monkeypatch):
    session, transport = AsyncMock(), AsyncMock()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: AsyncMock())
    session.__aenter__.return_value = session
    transport.__aenter__.return_value = (None, None, None)
    session.list_tools.return_value = mcp.types.ListToolsResult(
        tools=[mcp.types.Tool(name="dme_read_query", inputSchema={})]
    )
    monkeypatch.setattr(mcp, "ClientSession", lambda *args, **kwargs: session)
    monkeypatch.setattr(streamable_http, "streamable_http_client", lambda *a, **kw: transport)
    return session


async def test_mcp_compiled_query(tmp_path, mcp_backend):
    mcp_backend.call_tool.return_value = mcp.types.CallToolResult(
        content=[], structuredContent={"columns": ["n"], "rows": [{"n": 1}]}
    )
    payload, _ = await _run_task(tmp_path, params={"database": MCP_DATABASE})
    assert payload["rows_preview"] == [[1]] and payload["error"] is None
    queries = [call.args[1]["query"] for call in mcp_backend.call_tool.await_args_list]
    assert queries == [f"EXPLAIN PLAN FOR {EXPECTED_SQL}", EXPECTED_SQL]


async def test_mcp_row_preserves_explicit_null(tmp_path, mcp_backend):
    mcp_backend.call_tool.return_value = mcp.types.CallToolResult(
        content=[], structuredContent={"columns": ["n"], "rows": [{"n": None}]}
    )
    payload, _ = await _run_task(tmp_path, params={"database": MCP_DATABASE})
    assert payload["rows_preview"] == [[None]] and payload["error"] is None


async def test_mcp_row_missing_column_is_infrastructure_error(tmp_path, mcp_backend):
    mcp_backend.call_tool.return_value = mcp.types.CallToolResult(
        content=[], structuredContent={"columns": ["n"], "rows": [{"N": 1}]}
    )
    params = {"database": MCP_DATABASE, "validator": {"db_explain": False}}
    payload, _ = await _run_task(tmp_path, params=params)
    assert payload["source"] == "tool"
    assert "MCP SQL row missing column: 'n'" in payload["error"]


@pytest.mark.parametrize("stage", ["query", "session", "metadata"])
async def test_mcp_errors_reach_the_correct_layer(tmp_path, mcp_backend, stage):
    message = "Session terminated" if stage == "session" else "SQL rejected"
    error = mcp.shared.exceptions.McpError(
        mcp.types.ErrorData(code=32600 if stage == "session" else -32603, message=message)
    )
    getattr(mcp_backend, "list_tools" if stage == "metadata" else "call_tool").side_effect = error
    if stage == "query":
        service = build_sql_service("mcp", {"path": MCP_DATABASE["path"]})
        assert await asyncio.to_thread(service.explain, "SELECT 1") == message
        assert mcp_backend.call_tool.await_count == 1
    else:
        params = {"database": MCP_DATABASE, "validator": {"db_explain": stage == "metadata"}}
        payload, _ = await _run_task(tmp_path, params=params)
        assert payload["source"] == "tool" and message in payload["error"]
        assert mcp_backend.call_tool.await_count == (0 if stage == "metadata" else 1)


# --------------------------------------------------------------------------- helpers


async def _run_task(
    tmp_path, *, params=None, plugin_responses=None, semantic_client=None, outputs_dir=None
):
    """Run one delegated task through the full pipeline; return (reply payload, plugin model)."""
    db_path, schema_path = _sqlite_fixture(tmp_path)
    merged = {**_plugin_params(db_path, schema_path), **(params or {})}
    plugin_model = ScriptedModel(responses=list(plugin_responses or _PLUGIN_RESPONSES))
    parent_model = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "nl2sql", "description": "每个部门的员工人数"}),
            AIMessage(content="按部门统计的员工人数已经给出。"),
        ]
    )
    graph = _build_agent(
        merged,
        parent_model,
        plugin_model,
        semantic_client=semantic_client,
        outputs_dir=outputs_dir,
    )
    result = await graph.ainvoke({"messages": [HumanMessage(content="每个部门的员工人数")]})

    reply = next(message for message in result["messages"] if isinstance(message, ToolMessage))
    payload = json.loads(reply.content)
    assert isinstance(result["messages"][-1], AIMessage) and result["messages"][-1].content
    return payload, plugin_model


def _build_agent(params, parent_model, plugin_model, *, semantic_client=None, outputs_dir=None):
    extensions = _compile(
        params, plugin_model, semantic_client=semantic_client, outputs_dir=outputs_dir
    )
    assert [child["name"] for child in extensions["subagents"]] == ["nl2sql"]
    assert "runnable" in extensions["subagents"][0]
    return create_deep_agent(model=parent_model, **extensions)


def _compile(params, plugin_model, *, semantic_client=None, outputs_dir=None):
    settings = Settings.model_validate(
        {
            "models": {
                "primary": {
                    "name": "test-model",
                    "base_url": "https://example.invalid/v1",
                    "api_key": "test-secret",
                }
            },
            "plugins": {"enabled": ["nl2sql"], "config": {"nl2sql": params}},
        }
    )
    return compile_extensions(
        select_plugins(settings.plugins, [PLUGIN_ROOT]),
        plugin_params=dict(settings.plugins.config),
        model_resolver=lambda alias: plugin_model,
        semantic_client=semantic_client,
        outputs_dir=outputs_dir,
    )


def _plugin_params(db_path: Path, schema_path: Path) -> dict:
    return {
        "model": "primary",
        "database": {"db_id": "staff", "dialect": "sqlite", "path": str(db_path)},
        "perceptor": {"schema_mode": "full_schema", "user_schema": str(schema_path)},
        "generator": {"strategies": ["prompt"], "num_workers": 1, "num_samples": 1},
        "validator": {"db_explain": True},
        "reflector": {"threshold": 0.9, "ref_retries": 2},
        "executor": {"limit": -1, "preview_limit": 5},
        "selector": {"threshold": 0.9, "shortcut": -1},
    }


def _sqlite_fixture(tmp_path: Path) -> tuple[Path, Path]:
    db_path = tmp_path / "staff.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT, department TEXT, salary REAL);
            INSERT INTO employees (name, department, salary) VALUES
              ('Alice', 'Engineering', 120.0),
              ('Bob', 'Engineering', 110.0),
              ('Cara', 'Sales', 90.0),
              ('Dan', 'Legal', 95.0),
              ('Eve', 'HR', 80.0),
              ('Fay', 'Marketing', 85.0),
              ('Gus', 'Finance', 88.0);
            """
        )
    schema_path = tmp_path / "schema.md"
    schema_path.write_text(
        "CREATE TABLE employees (id INTEGER PRIMARY KEY, name TEXT, department TEXT, salary REAL);\n",
        encoding="utf-8",
    )
    return db_path, schema_path


def test_standalone_nl2sql_yaml_and_yml(tmp_path):
    from nl2sql.config_manager import NL2SQLConfig

    for suffix in (".yaml", ".yml"):
        path = tmp_path / ("standalone" + suffix)
        path.write_text("# standalone parameters\nmodel: primary\ndatabase:\n  dialect: sqlite\n")
        assert NL2SQLConfig.load(path).get("model") == "primary"
    legacy = tmp_path / "standalone.json"
    legacy.write_text('{"model": "primary", "database": {"dialect": "sqlite"}}')
    with pytest.raises(ValueError, match="YAML extension required"):
        NL2SQLConfig.load(legacy)


def _perceptor(**options):
    from nl2sql.config_manager import NL2SQLConfig
    from nl2sql.nodes.perceptor import PerceptorNode

    return PerceptorNode(
        config=NL2SQLConfig(
            {"model": "test", "database": {"dialect": "sqlite", "db_id": "schools"}}
        ),
        **options,
    )


async def test_bird_perceptor_loads_evidence_before_retrieve(tmp_path):
    from nl2sql.state import get_default_state

    evidence = "rate = meals / enrollment"
    path = tmp_path / "evidence.md"
    path.write_text(evidence)
    ddl = "CREATE TABLE schools (`id` INTEGER PRIMARY KEY); -- matched values retained\n"
    requests_seen = []

    def handler(request):
        assert request.url.path == "/api/semantic/v1/semantic/retrieve"
        requests_seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "schemaString": ddl,
                "dataAccessPlan": {
                    "tables": [
                        {
                            "table": "schools",
                            "columns": [
                                {"name": "id", "valueType": "INTEGER", "description": "school id"}
                            ],
                        }
                    ]
                },
            },
        )

    node = _perceptor(schema_mode="bird", user_evidence=str(path))
    question = "Question: Find schools\nEvidence: " + evidence
    with SemanticServiceClient(
        base_url="http://semantic.test", transport=httpx.MockTransport(handler)
    ) as client:
        node.bind_semantic_client(client)
        result = await node.run(get_default_state(question))
    assert requests_seen == [{"query": question, "db_name": "schools", "evidence": evidence}]
    assert result["schema_str"] == ddl
    assert result["schema"]["schools"]["columns"]["id"]["value_type"] == "INTEGER"
    assert result["evidence"] == evidence


@pytest.mark.parametrize("evidence", ["", "explicit evidence"])
async def test_evidence_override_bypasses_file(evidence):
    from nl2sql.state import get_default_state

    node = _perceptor(schema_mode="bird", user_evidence="missing-file")
    result = await node.run(
        get_default_state(
            "question",
            schema_str="external schema",
            evidence=evidence,
            context_overrides=["schema_str", "evidence"],
        )
    )
    assert result["schema_str"] == "external schema"
    assert result["evidence"] == evidence


@pytest.mark.parametrize("bundle", [{}, {"schemaString": None}, {"schemaString": "  "}])
async def test_bird_rejects_missing_ddl_without_fallback(bundle):
    from nl2sql.errors import DataAgentError
    from nl2sql.state import get_default_state

    def handler(request):
        assert request.url.path.endswith("/semantic/retrieve")
        return httpx.Response(200, json=bundle)

    node = _perceptor(schema_mode="bird")
    with SemanticServiceClient(
        base_url="http://semantic.test", transport=httpx.MockTransport(handler)
    ) as client:
        node.bind_semantic_client(client)
        with pytest.raises(DataAgentError, match="schemaString"):
            await node.run(get_default_state("Find schools"))


def test_perceptor_rejects_unknown_modes_and_missing_bird_database():
    from nl2sql.config_manager import NL2SQLConfig
    from nl2sql.errors import DataAgentError
    from nl2sql.nodes.perceptor import PerceptorNode

    with pytest.raises(ValueError, match="schema_mode"):
        _perceptor(schema_mode="unknown")
    node = PerceptorNode(
        config=NL2SQLConfig({"model": "test", "database": {"dialect": "sqlite"}}),
        schema_mode="bird",
    )
    with pytest.raises(DataAgentError, match="database.db_id"):
        node.bird_schema("Find schools", "")


async def test_bird_mode_through_compiled_plugin(tmp_path):
    requests_seen = []
    ddl = "CREATE TABLE employees (department TEXT);"

    def handler(request):
        requests_seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "schemaString": ddl,
                "dataAccessPlan": {
                    "tables": [
                        {
                            "table": "employees",
                            "columns": [{"name": "department", "valueType": "TEXT"}],
                        }
                    ]
                },
            },
        )

    with SemanticServiceClient(
        base_url="http://semantic.test", transport=httpx.MockTransport(handler)
    ) as client:
        payload, model = await _run_task(
            tmp_path,
            params={"perceptor": {"schema_mode": "bird"}},
            semantic_client=client,
        )
    assert payload["error"] is None
    assert payload["sql"] == EXPECTED_SQL
    assert payload["row_count"] == 6
    assert len(requests_seen) == 1
    assert requests_seen[0]["evidence"] == ""
    assert len(model.requests) == 3
    assert ddl in model.requests[0][1].content


@pytest.mark.parametrize(
    ("emit_reasoning_tag", "root_tag"),
    [(None, None), (False, None), (True, None), (True, False), (False, True)],
)
@pytest.mark.parametrize("sql_field", ["sql", "expression"])
async def test_generator_online_icl_and_shared_rules(sql_field, emit_reasoning_tag, root_tag):
    from collections import Counter

    from nl2sql.config_manager import NL2SQLConfig
    from nl2sql.graph import build_nl2sql_graph
    from nl2sql.state import get_default_state

    config = NL2SQLConfig(
        {
            "model": "primary",
            "database": {"dialect": "sqlite"},
            "nodes": ["perceptor", "generator"],
            **({} if root_tag is None else {"emit_reasoning_tag": root_tag}),
            "generator": {
                "strategies": ["prompt", "dc", "skeleton", "icl"],
                "num_workers": 3,
                "num_samples": 2,
                **(
                    {} if emit_reasoning_tag is None else {"emit_reasoning_tag": emit_reasoning_tag}
                ),
            },
        }
    )
    requests_seen = []

    def handler(request):
        assert request.url.path.endswith("/advanced-search/sql-few-shots")
        requests_seen.append(dict(request.url.params))
        return httpx.Response(
            200,
            json=[
                {"query": f"example question {i}", sql_field: f"SELECT {i}"} for i in range(1, 6)
            ],
        )

    # One generic fake response supports both the prompt and XML strategy parsers.
    reply = AIMessage(
        content="```sql\nSELECT 1\n```\n```sql\nSELECT 2\n```\n<result>SELECT 3</result>"
    )
    if emit_reasoning_tag:
        reply = AIMessage(content="<reasoning>Example analysis</reasoning>\n" + reply.content)
    model = ScriptedModel(responses=[reply] * 24)
    with SemanticServiceClient(
        base_url="http://semantic.test", transport=httpx.MockTransport(handler)
    ) as client:
        graph = build_nl2sql_graph(config, model=model, semantic_client=client)
        # Even an identical question in a new run must retrieve again; no node-level cache.
        for run in range(2):
            state = get_default_state(
                "count records",
                schema_str="CREATE TABLE records (id INTEGER)",
                sql_rules="COMMON_RULE",
                few_shot_examples="ARCHIVED_EXAMPLE",
                context_overrides=["schema_str", "sql_rules", "few_shot_examples"],
            )
            result = await graph.ainvoke(state)
            assert requests_seen == [{"query": "count records", "topK": "5"}] * (run + 1)
            assert Counter(x.strategy for x in result["generation_results"]) == {
                "prompt": 6,
                "dc": 3,
                "skeleton": 3,
                "icl": 3,
            }
            for candidate in result["generation_results"]:
                assert "COMMON_RULE" in candidate.prompt
                assert ("<reasoning>" in candidate.prompt) == (
                    root_tag
                    if root_tag is not None
                    else bool(emit_reasoning_tag) and candidate.strategy != "prompt"
                )
                if candidate.strategy != "prompt":
                    assert candidate.sql == "SELECT 3"
                if candidate.strategy == "icl":
                    assert "ARCHIVED_EXAMPLE" not in candidate.prompt
                    assert candidate.prompt.count("- Example ") == 5
                    for i in range(1, 6):
                        assert (
                            f"Question: example question {i}\nSQL: SELECT {i}" in candidate.prompt
                        )
            assert "ARCHIVED_EXAMPLE" not in result["few_shot_examples"]
    assert len(model.requests) == 24


@pytest.mark.parametrize("retrieval", [[], "failure"])
async def test_icl_empty_or_failed_retrieval_never_uses_archive(retrieval):
    from unittest.mock import Mock

    from nl2sql.config_manager import NL2SQLConfig
    from nl2sql.graph import build_nl2sql_graph
    from nl2sql.state import get_default_state

    config = NL2SQLConfig(
        {
            "model": "primary",
            "database": {"dialect": "sqlite"},
            "nodes": ["perceptor", "generator"],
            "generator": {"strategies": ["icl", "dc"], "num_workers": 2},
        }
    )
    client = Mock()
    if retrieval == "failure":
        client.sql_few_shots.side_effect = ValueError("service unavailable")
    else:
        client.sql_few_shots.return_value = retrieval
    model = ScriptedModel(responses=[AIMessage(content="<result>SELECT 1</result>")] * 4)
    graph = build_nl2sql_graph(config, model=model, semantic_client=client)
    state = get_default_state(
        "question",
        schema_str="CREATE TABLE t (v INT)",
        few_shot_examples="ARCHIVED_EXAMPLE",
        context_overrides=["schema_str", "few_shot_examples"],
    )
    updates = [update async for update in graph.astream(state, stream_mode="updates")]
    perceptor = updates[0]["perceptor"]
    assert perceptor["few_shot_examples"] == ""
    assert bool(perceptor.get("few_shot_error")) == (retrieval == "failure")
    candidates = updates[1]["generator"]["generation_results"]
    client.sql_few_shots.assert_called_once_with("question", limit=5)
    assert all("ARCHIVED_EXAMPLE" not in x.prompt for x in candidates)
    assert len(candidates) == (2 if retrieval == "failure" else 4)
    if retrieval == "failure":
        assert all(x.strategy == "dc" for x in candidates)


async def test_perceptor_without_icl_does_not_retrieve():
    from unittest.mock import Mock

    from nl2sql.config_manager import NL2SQLConfig
    from nl2sql.nodes.perceptor import PerceptorNode
    from nl2sql.state import get_default_state

    node = PerceptorNode(
        config=NL2SQLConfig(
            {
                "model": "primary",
                "database": {"dialect": "sqlite"},
                "generator": {"strategies": ["dc"]},
            }
        )
    )
    client = Mock()
    node.bind_semantic_client(client)
    await node.run(get_default_state("question", context_overrides=["schema_str"]))
    client.sql_few_shots.assert_not_called()


async def test_icl_requires_host_semantic_client():
    from nl2sql.config_manager import NL2SQLConfig
    from nl2sql.errors import DataAgentError
    from nl2sql.graph import build_nl2sql_graph
    from nl2sql.state import get_default_state

    config = NL2SQLConfig(
        {
            "model": "primary",
            "database": {"dialect": "sqlite"},
            "nodes": ["perceptor", "generator"],
            "generator": {"strategies": ["icl"], "num_workers": 2},
        }
    )
    graph = build_nl2sql_graph(config, model=ScriptedModel(responses=[]))
    with pytest.raises(DataAgentError, match="ICL") as exc:
        await graph.ainvoke(get_default_state("question", context_overrides=["schema_str"]))
    assert exc.value.source == "config"


@pytest.mark.parametrize("value", ["true", "false", 0, 1, None])
def test_generator_reasoning_tag_requires_boolean(value):
    from nl2sql.config_manager import NL2SQLConfig

    with pytest.raises(ValueError, match="generator.emit_reasoning_tag"):
        NL2SQLConfig(
            {
                "model": "primary",
                "database": {"dialect": "sqlite"},
                "generator": {"emit_reasoning_tag": value},
            }
        )


@pytest.mark.parametrize(
    "wrapper",
    [
        "<result>{sql}</result>",
        "<RESULT>{sql}</RESULT>",
        "<result>```sql\n{sql}\n```</result>",
        "<result>```SQL\n{sql}\n```</result>",
        "<result>```\n{sql}\n```</result>",
        "<result>{sql}",
        "</reasoning>\n[result>{sql}",
    ],
)
def test_xml_sql_parser_preserves_sql(wrapper):
    from nl2sql.utils.nl2sql_utils import xml_parser

    sql = "SELECT 'a--b', 'a/*b*/c', 'a  b', '```sql', '&lt;', '<result>'\nFROM t -- comment\nWHERE x < 2;"
    assert xml_parser(wrapper.format(sql=sql)) == [sql]


@pytest.mark.parametrize(
    "content",
    ["SELECT 1", "```sql\nSELECT 1\n```", "<result></result>", "<result>```sql\n```</result>"],
)
def test_xml_sql_parser_requires_nonempty_result(content):
    from nl2sql.utils.nl2sql_utils import xml_parser

    assert xml_parser(content) == []


@pytest.mark.parametrize("dc_failures", [3, 4])
async def test_generator_parse_retry_preserves_strategy(monkeypatch, dc_failures):
    from collections import Counter

    import nl2sql.nodes.generator as generator_module
    from nl2sql.config_manager import NL2SQLConfig
    from nl2sql.nodes.generator import GeneratorNode
    from nl2sql.prompt_manager import PromptTemplate
    from nl2sql.state import get_default_state

    node = GeneratorNode(
        config=NL2SQLConfig({"model": "primary", "database": {"dialect": "sqlite"}}),
        strategies=["dc", "skeleton", "icl"],
        num_workers=1,
    )
    node.bind_model(ScriptedModel(responses=[]))
    prompts = {
        PromptTemplate.from_package_relative(
            f"prompts/generator/{strategy}_system"
        ).apply_prompt_template(
            dialect="sqlite",
            sql_rules="",
            emit_reasoning_tag=False,
        ): strategy
        for strategy in node.strategies
    }
    counts = Counter()
    requests = {}

    async def collect(model, messages):
        strategy = prompts[messages[0].content]
        counts[strategy] += 1
        contents = [m.content for m in messages]
        if strategy in requests:
            assert contents == requests[strategy]
        requests[strategy] = contents
        if strategy == "dc" and counts[strategy] <= dc_failures:
            return AIMessage(content="No result tag")
        return AIMessage(content=f"<result>SELECT '{strategy}'</result>")

    monkeypatch.setattr(generator_module, "collect_model_stream", collect)
    result = await node.run(get_default_state("question", few_shot_examples="EXAMPLE_FROM_STATE"))
    assert counts == {"dc": 4, "skeleton": 1, "icl": 1}
    assert {r.strategy: r.sql for r in result["generation_results"]} == {
        strategy: f"SELECT '{strategy}'"
        for strategy in node.strategies
        if strategy != "dc" or dc_failures == 3
    }
    assert "EXAMPLE_FROM_STATE" in requests["icl"][1]


@pytest.mark.parametrize("strategy", ["prompt", "dc", "skeleton", "icl"])
@pytest.mark.parametrize(
    "options,retries", [({}, 3), ({"parse_retries": 0}, 0), ({"parse_retries": 5}, 5)]
)
async def test_generator_parse_retry_limit(strategy, options, retries):
    from nl2sql.config_manager import NL2SQLConfig
    from nl2sql.errors import DataAgentError
    from nl2sql.nodes.generator import GeneratorNode

    config = NL2SQLConfig(
        {"model": "primary", "database": {"dialect": "sqlite"}, "generator": options}
    )
    node = GeneratorNode(config=config, **config.section("generator"))
    model = ScriptedModel(responses=[AIMessage(content="No SQL")] * (retries + 1))
    node.bind_model(model)
    with pytest.raises(DataAgentError, match=f"SQL parsing failed after {retries} retries"):
        await node.generate_with_llm(
            strategy,
            {"dialect": "sqlite", "sql_rules": "", "emit_reasoning_tag": False, "num_samples": 1},
            {"question": "q", "schema": "", "evidence": "", "few_shot_examples": ""},
        )
    assert len(model.requests) == retries + 1


async def test_generator_retries_truncated_output(monkeypatch):
    import nl2sql.nodes.generator as generator_module
    from nl2sql.config_manager import NL2SQLConfig
    from nl2sql.nodes.generator import GeneratorNode

    node = GeneratorNode(
        config=NL2SQLConfig({"model": "primary", "database": {"dialect": "sqlite"}})
    )
    node.bind_model(ScriptedModel(responses=[]))
    responses = [
        AIMessage(content="<result>SELECT", response_metadata={"finish_reason": "length"}),
        AIMessage(content="<result>SELECT 1</result>"),
    ]

    async def collect(model, messages):
        return responses.pop(0)

    monkeypatch.setattr(generator_module, "collect_model_stream", collect)
    result = await node.generate_with_llm(
        "dc",
        {"dialect": "sqlite", "sql_rules": "", "emit_reasoning_tag": False},
        {"question": "q", "schema": "", "evidence": ""},
    )
    assert result[0][0] == "SELECT 1"
    assert responses == []


async def test_generator_does_not_retry_transport_errors(monkeypatch):
    from unittest.mock import AsyncMock

    import nl2sql.nodes.generator as generator_module
    from nl2sql.config_manager import NL2SQLConfig
    from nl2sql.nodes.generator import GeneratorNode

    node = GeneratorNode(
        config=NL2SQLConfig({"model": "primary", "database": {"dialect": "sqlite"}})
    )
    node.bind_model(ScriptedModel(responses=[]))
    collect = AsyncMock(side_effect=RuntimeError("transport failed"))
    monkeypatch.setattr(generator_module, "collect_model_stream", collect)
    with pytest.raises(RuntimeError, match="transport failed"):
        await node.generate_with_llm(
            "dc",
            {"dialect": "sqlite", "sql_rules": "", "emit_reasoning_tag": False},
            {"question": "q", "schema": "", "evidence": ""},
        )
    assert collect.await_count == 1
