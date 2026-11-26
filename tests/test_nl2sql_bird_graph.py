"""BIRD checker graph configuration and native Selector integration."""

import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from conftest import ScriptedModel
from langchain_core.messages import AIMessage, HumanMessage
from test_nl2sql_plugin import _compile, _plugin_params, _sqlite_fixture

from dataagent.bootstrap import LaunchOptions, prepare_runtime
from dataagent.client import create_agent

NODES = ["perceptor", "generator", "bird-validator", "selector"]


def bird_config(db, schema):
    common = _plugin_params(db, schema)
    return {k: common[k] for k in ("model", "database", "perceptor", "generator")} | {
        "nodes": NODES
    }


def bird_runtime(tmp_path, params):
    settings = {
        "schema_version": 4,
        "models": {
            "primary": {
                "name": "test-model",
                "base_url": "https://example.invalid/v1",
                "api_key": "test",
            }
        },
        "dataagent": {"entry": "nl2sql"},
        "plugins": {"enabled": ["nl2sql"], "config": {"nl2sql": params}},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(settings))
    return prepare_runtime(LaunchOptions(cwd=tmp_path, config=config_path))


@pytest.mark.parametrize(
    "selector_replies", [[], [AIMessage(content='```json\n[{"score":0.2,"issues":[]}]\n```')]]
)
async def test_host_yaml_bird_graph_uses_native_selector(tmp_path, selector_replies):
    db, schema = _sqlite_fixture(tmp_path)
    params = bird_config(db, schema)
    # A positive shortcut counts valid votes. Otherwise the native selector scores with the model.
    params["selector"] = {"shortcut": -1 if selector_replies else 1}
    runtime = bird_runtime(tmp_path, params)
    sql = "SELECT salary FROM employees"
    model = ScriptedModel(responses=[AIMessage(content=f"```sql\n{sql}\n```"), *selector_replies])

    async with create_agent(runtime, model=model) as agent:
        state = await agent.ainvoke({"messages": [HumanMessage(content="Show salaries")]})
    payload = json.loads(state["messages"][-1].content)
    assert payload["executed"] is True
    assert payload["sql"] == sql
    assert payload["row_count"] == 7
    assert Path(payload["result_file"]).is_file()
    assert len(model.requests) == 1 + len(selector_replies)
    assert payload["confidence"] == (0.2 if selector_replies else 1)


async def test_host_yaml_runs_selected_checkers_in_order_on_latest_sql(tmp_path):
    db, schema = _sqlite_fixture(tmp_path)
    params = bird_config(db, schema)
    params["bird-validator"] = {"checkers": ["select", "syntax"]}
    params["selector"] = {"shortcut": 1}
    runtime = bird_runtime(tmp_path, params)
    original = "SELECT employees.* FROM employees"
    first_repair = "SELECT unknown_salary FROM employees"
    final_sql = "SELECT salary FROM employees"
    model = ScriptedModel(
        responses=[
            AIMessage(content=f"```sql\n{original}\n```"),
            AIMessage(content=f"<result>{first_repair}</result>"),
            AIMessage(content=f"<result>{final_sql}</result>"),
        ]
    )

    async with create_agent(runtime, model=model) as agent:
        state = await agent.ainvoke({"messages": [HumanMessage(content="Show salaries")]})
    payload = json.loads(state["messages"][-1].content)
    assert payload["sql"] == final_sql
    assert payload["row_count"] == 7
    _, select_request, syntax_request = model.requests
    assert original in select_request[0].content and "wildcard" in select_request[0].content
    assert first_repair in syntax_request[0].content
    assert "no such column: unknown_salary" in syntax_request[0].content


@pytest.mark.parametrize("checkers", [[], ["syntax"]])
async def test_omitting_select_still_returns_executed_sql_to_selector(tmp_path, checkers):
    db, schema = _sqlite_fixture(tmp_path)
    params = bird_config(db, schema)
    params["bird-validator"] = {"checkers": checkers}
    params["selector"] = {"shortcut": 1}
    runtime = bird_runtime(tmp_path, params)
    sql = "SELECT employees.* FROM employees"
    model = ScriptedModel(responses=[AIMessage(content=f"```sql\n{sql}\n```")])

    async with create_agent(runtime, model=model) as agent:
        state = await agent.ainvoke({"messages": [HumanMessage(content="Show employees")]})
    payload = json.loads(state["messages"][-1].content)
    assert payload["sql"] == sql
    assert payload["row_count"] == 7
    assert payload["confidence"] == 1
    assert len(model.requests) == 1


def test_host_marks_bird_graph_as_executing(tmp_path):
    db, schema = _sqlite_fixture(tmp_path)
    ext = _compile(bird_config(db, schema), ScriptedModel(responses=[]))
    assert "query and execute" in ext["subagents"][0]["description"]


@pytest.mark.parametrize(
    "override",
    [
        {"bird-validator": {"temperature": 0.3}},
        {"bird-validator": {"checkers": None}},
        {"bird-validator": {"checkers": "syntax"}},
        {"bird-validator": {"checkers": [True]}},
        {"bird-validator": {"checkers": ["missing"]}},
        {"bird-validator": {"checkers": ["result", "result"]}},
        {"bird-validator": {"repair_attempts": False}},
        {"bird-validator": {"preview_limit": -2}},
        {"bird-validator": {"sql_security_enabled": "false"}},
        {"bird-validator": {"db_explain": False}},
        {"selector": {"sel_retries": 1}},
        {"executor": {"limit": 10}},
        {"reflector": {}},
        {"validator": {}},
        {"nodes": NODES[:-1]},
        {"nodes": ["perceptor", "generator"], "bird-validator": {}},
    ],
)
def test_incompatible_bird_config_is_rejected(override):
    from nl2sql.config_manager import NL2SQLConfig

    with pytest.raises(ValueError, match="bird-validator"):
        NL2SQLConfig({"model": "m", "database": {"dialect": "sqlite"}, "nodes": NODES, **override})


@pytest.mark.parametrize("security_enabled", [None, False, True], ids=["default", "off", "on"])
async def test_bird_sql_security_switch_before_execution(tmp_path, monkeypatch, security_enabled):
    db, schema = _sqlite_fixture(tmp_path)
    # Exists in the database but is absent from the allowed schema.
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE unapproved_table (salary REAL)")
        conn.execute("INSERT INTO unapproved_table VALUES (7.0)")
    params = bird_config(db, schema)
    params["bird-validator"] = {"repair_attempts": 1}
    if security_enabled is not None:
        params["bird-validator"]["sql_security_enabled"] = security_enabled
    params["selector"] = {"shortcut": 1}
    runtime = bird_runtime(tmp_path, params)

    statements = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    sql = "SELECT salary FROM unapproved_table WHERE salary > 0"
    responses = [AIMessage(content=f"```sql\n{sql}\n```")]
    if security_enabled:
        responses.extend(AIMessage(content=f"<result>{sql}</result>") for _ in range(4))
    model = ScriptedModel(responses=responses)
    async with create_agent(runtime, model=model) as agent:
        state = await agent.ainvoke({"messages": [HumanMessage(content="Show salaries")]})
    payload = json.loads(state["messages"][-1].content)
    candidate_executed = any("unapproved_table" in stmt for stmt in statements)
    if security_enabled:
        assert payload["source"] == "constraint"
        assert "sql" not in payload
        assert not candidate_executed
        assert not list(runtime.paths.output_root.rglob("*.csv"))
    else:
        assert candidate_executed
        assert payload["sql"] == sql
        assert payload["row_count"] == 1


async def test_host_passes_provider_thinking_options_to_repair_prompt(tmp_path):
    class ThinkingModel(ScriptedModel):
        model_kwargs: dict

    db, schema = _sqlite_fixture(tmp_path)
    params = bird_config(db, schema)
    params["selector"] = {"shortcut": 1}
    runtime = bird_runtime(tmp_path, params)
    model = ThinkingModel(
        model_kwargs={"extra_body": {"enable_thinking": True}},
        responses=[
            AIMessage(content="```sql\nSELECT absent FROM employees\n```"),
            AIMessage(content="<result>SELECT salary FROM employees</result>"),
        ],
    )
    async with create_agent(runtime, model=model) as agent:
        state = await agent.ainvoke({"messages": [HumanMessage(content="Show salaries")]})
    payload = json.loads(state["messages"][-1].content)
    assert payload["sql"] == "SELECT salary FROM employees"
    assert payload["row_count"] == 7
    assert len(model.requests) == 2
    prompt = model.requests[1][0].content
    assert "<result>" in prompt
    assert "<reasoning>" not in prompt
