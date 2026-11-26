"""Plugin-owned configuration and reuse of the shared database service."""

import subprocess
import sys
from pathlib import Path
from threading import Event

import pytest
import yaml
from conftest import ScriptedModel
from langchain_core.messages import AIMessage

from dataagent import LaunchOptions, build_sql_service, create_agent, prepare_runtime
from dataagent.assembly.context import AgentContext
from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/nl2sql-react"


def configured(tmp_path, *, database=None, enabled=True):
    data = {
        "models": {
            "primary": {
                "name": "offline",
                "base_url": "https://example.invalid/v1",
                "api_key": "unused",
            }
        },
        "plugins": {
            "enabled": ["nl2sql-react"] if enabled else [],
            "config": {
                "nl2sql-react": {
                    "database": database
                    or {
                        "engine": "sqlite",
                        "dialect": "sqlite",
                        "db_id": "fixture",
                        "path": str(tmp_path / "fixture.sqlite"),
                    }
                }
            },
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))


def test_plugin_database_uses_existing_context_without_host_datasources(tmp_path):
    runtime = configured(tmp_path)
    context = AgentContext(runtime)
    read = PythonLoader().load(PLUGIN, "nl2sql_react/tools/database.py:configuration")
    config, source = read(context)
    assert not hasattr(runtime.settings, "datasources")
    assert not hasattr(context, "get_datasource")
    assert config.database.db_id == "fixture"
    assert source["connection"]["path"] == str(tmp_path / "fixture.sqlite")
    source["connection"]["path"] = "changed"
    assert read(context)[1]["connection"]["path"] == str(tmp_path / "fixture.sqlite")


def test_relative_database_path_is_rejected_instead_of_using_launch_directory(tmp_path):
    runtime = configured(tmp_path, database={"path": "./fixture.sqlite"})
    read = PythonLoader().load(PLUGIN, "nl2sql_react/tools/database.py:configuration")
    with pytest.raises(ValueError, match="absolute database path"):
        read(AgentContext(runtime))


def test_local_diagnostics_redact_plugin_owned_connection(tmp_path):
    runtime = configured(tmp_path)
    context = AgentContext(runtime)
    secrets = PythonLoader().load(PLUGIN, "nl2sql_react/tools/database.py:redaction_secrets")(
        context
    )
    assert str(tmp_path / "fixture.sqlite") in secrets
    assert str(tmp_path / "fixture.sqlite") not in context.redaction_secrets


def test_local_example_loads_without_host_configuration_extensions(tmp_path, monkeypatch):
    for name, value in {
        "LLM_MODEL": "offline",
        "LLM_BASE_URL": "https://example.invalid/v1",
        "LLM_API_KEY": "unused",
        "BIRD_DB_ID": "fixture",
        "NL2SQL_DATABASE_PATH": str(tmp_path / "fixture.sqlite"),
    }.items():
        monkeypatch.setenv(name, value)
    runtime = prepare_runtime(
        LaunchOptions(
            cwd=tmp_path, config=PLUGIN.parents[2] / "examples/nl2sql-react/config.example.yaml"
        )
    )
    _, source = PythonLoader().load(PLUGIN, "nl2sql_react/tools/database.py:configuration")(
        AgentContext(runtime)
    )
    assert source["db_id"] == "fixture"
    assert runtime.settings.plugins.enabled == ("nl2sql-react",)


async def test_disabled_local_does_not_import_plugin_or_legacy_driver(tmp_path, monkeypatch):
    runtime = configured(tmp_path, enabled=False)
    original = PythonLoader.load

    def checked(self, root, entrypoint):
        assert Path(root).resolve() != PLUGIN.resolve()
        return original(self, root, entrypoint)

    monkeypatch.setattr(PythonLoader, "load", checked)
    model = ScriptedModel(responses=[AIMessage(content="Original host behavior")])
    async with create_agent(runtime, model=model) as agent:
        result = await agent.ainvoke({"messages": [("user", "Hello")]})
    assert result["messages"][-1].content == "Original host behavior"


def test_local_preserves_special_database_paths(tmp_path):
    import sqlite3

    loader = PythonLoader()
    options = loader.load(PLUGIN, "nl2sql_react/tools/database.py:Config")
    run = loader.load(PLUGIN, "nl2sql_react/tools/database.py:_run")
    path = tmp_path / "odd # % ? name.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE values_test(n INTEGER); INSERT INTO values_test VALUES(42)")
    result = run(
        options(database={"path": str(path)}),
        {"engine": "sqlite", "connection": {"path": str(path)}},
        "execute_sql",
        {"sql": "SELECT n FROM values_test"},
        Event(),
    )
    assert result["rows"] == [[42]]


def test_react_can_load_without_the_original_nl2sql_agent(tmp_path):
    from dataagent.assembly.graph import load_extensions

    runtime = configured(tmp_path)
    compiled = load_extensions(runtime)
    assert [child["name"] for child in compiled["subagents"]] == ["nl2sql-react"]
    assert {tool.name for tool in compiled["tools"]} == {"nl2sql-react__select_result"}


def test_local_registers_after_de_agent_nl2sql_hook_in_the_same_process(tmp_path):
    configured(tmp_path)
    script = """
import sys
from pathlib import Path

from dataagent import LaunchOptions, prepare_runtime
from dataagent.assembly.graph import load_extensions
from dataagent.extensions.loading import PythonLoader

plugins, workdir = map(Path, sys.argv[1:])
PythonLoader().load(plugins / "de-agent", "hooks/nl2sql.py:prepare_input")
runtime = prepare_runtime(LaunchOptions(cwd=workdir, config=workdir / "config.yaml"))
compiled = load_extensions(runtime)
local = next(child for child in compiled["subagents"] if child["name"] == "nl2sql-react")
assert "nl2sql-react__review_query" in {tool.name for tool in local["tools"]}
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(PLUGIN.parent), str(tmp_path)],
        cwd=PLUGIN.parents[2],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_both_plugins_can_compile_and_use_the_shared_sqlite_driver(tmp_path):
    import sqlite3

    from dataagent.assembly.graph import load_extensions

    runtime = configured(tmp_path)
    path = tmp_path / "config.yaml"
    config = yaml.safe_load(path.read_text())
    config["plugins"]["enabled"] = ["nl2sql", "nl2sql-react"]
    config["plugins"]["config"]["nl2sql"] = {
        "model": "primary",
        "database": dict(config["plugins"]["config"]["nl2sql-react"]["database"]),
    }
    path.write_text(yaml.safe_dump(config))
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))
    model = ScriptedModel(responses=[])
    context = AgentContext(runtime, model=model)
    compiled = load_extensions(runtime, model_resolver=context.get_model, context=context)
    assert {child["name"] for child in compiled["subagents"]} == {"nl2sql", "nl2sql-react"}
    assert {tool.name for tool in compiled["tools"]} == {"nl2sql-react__select_result"}
    database = tmp_path / "fixture.sqlite"
    with sqlite3.connect(database) as db:
        db.executescript("CREATE TABLE original(n INTEGER); INSERT INTO original VALUES(7),(8),(9)")
    with build_sql_service("sqlite", {"path": str(database)}) as service:
        assert service.explain("SELECT n FROM original") is None
        assert "no such column" in service.explain("SELECT missing FROM original")
        assert "no such column" in service.execute("SELECT missing FROM original")[2]
        query = "SELECT n FROM original ORDER BY n"
        assert service.execute(query, limit=-1) == (["n"], [(7,), (8,), (9,)], None)
        assert service.execute(query, limit=1) == (["n"], [(7,), (8,)], None)
        assert service.execute(query + " LIMIT 0") == (["n"], [], None)


def test_local_tools_use_the_public_runtime_without_registering_new_root_tools(tmp_path):
    from dataagent.data_gateway.sql_service import build_sql_service
    from dataagent.db_explore import recall_database

    loader = PythonLoader()
    assert (
        loader.load(PLUGIN, "nl2sql_react/tools/database.py:build_sql_service") is build_sql_service
    )
    assert loader.load(PLUGIN, "nl2sql_react/tools/database.py:recall_database") is recall_database
    runtime = configured(tmp_path, enabled=False)
    assert runtime.settings.plugins.enabled == ()
    assert not hasattr(runtime.settings, "datasources")
