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

import csv
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from dataagent.core.graph import create_deep_agent
from dataagent.extensions import compile_extensions, select_plugins
from dataagent.semantic_client import SemanticServiceClient
from dataagent.settings import Settings

PROJECT = Path(__file__).resolve().parents[1]
BUILTIN_ROOT = PROJECT / "builtin-plugins"
PLUGIN_ROOT = BUILTIN_ROOT / "nl2sql"

# Direct ``nl2sql`` imports in the unit tests below need the plugin root
# importable; the compiled-subagent pipeline needs no such global entry.
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

EXPECTED_SQL = "SELECT department, COUNT(*) AS cnt FROM employees GROUP BY department ORDER BY cnt DESC"

_PLUGIN_RESPONSES = [
    AIMessage(content=f"```sql\n{EXPECTED_SQL}\n```"),  # generator
    AIMessage(content='```json\n[{"id": 0, "score": 1, "issues": []}]\n```'),  # validator: validate_semantic
    AIMessage(content='```json\n[{"score": 1, "issues": []}]\n```'),  # selector
]


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
    assert "result_file" in payload["rows_note"] and "do not dispatch the question again" in payload["rows_note"]

    meta = json.loads(result_file.with_name(f"{payload['query_id']}.meta.json").read_text(encoding="utf-8"))
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
        tmp_path, outputs_dir=outputs, plugin_responses=[
            AIMessage(content="```sql\nSELECT missing_column FROM employees\n```"),
            AIMessage(content='```json\n[{"id": 0, "score": 1, "issues": []}]\n```'),
            AIMessage(content='```json\n[{"score": 1, "issues": []}]\n```'),
        ], params={"validator": {"db_explain": False}},
    )

    assert payload["error"]
    assert "result_file" not in payload
    assert not (outputs / "nl2sql").exists()


async def test_model_call_budget_surfaces_as_error_reply(tmp_path):
    payload, plugin_model = await _run_task(
        tmp_path, params={"model_call_budget": 1}, plugin_responses=_PLUGIN_RESPONSES[:1]
    )

    # the generator consumed the only budgeted call; the validator's call is refused
    assert payload["error"]
    assert payload["source"] == "llm"
    assert len(plugin_model.requests) == 1


async def test_task_builds_schema_from_host_semantic_client(tmp_path):
    """The perceptor reads its schema through the host-injected client.

    The legacy ``semantic`` plugin section is ignored: no client is built from
    it — every request is served by the client supplied to compile_extensions.
    """
    requests_seen = []

    def handler(request):
        requests_seen.append(request.url.path)
        if request.url.path.endswith("/advanced-search/table-list"):
            return httpx.Response(200, json=[{"staff.employees": {"table_description": "员工表"}}])
        if request.url.path.endswith("/advanced-search/table-columns-info"):
            return httpx.Response(200, json={
                "staff.employees.department": {
                    "column_short_description": "部门", "value_type": "TEXT",
                    "value_description": "Engineering",
                },
                "staff.employees.salary": {
                    "column_short_description": "薪资", "value_type": "REAL",
                    "value_description": "120.0",
                },
            })
        return httpx.Response(200, json=[])

    client = SemanticServiceClient("http://semantic.internal", transport=httpx.MockTransport(handler))
    with client:
        payload, _ = await _run_task(
            tmp_path,
            params={
                "perceptor": {"schema_mode": "full_schema"},  # no user_schema: schema from the service
                "semantic": {"base_url": "http://plugin-config.invalid"},  # legacy section, ignored
            },
            semantic_client=client,
        )

    assert payload["sql"] == EXPECTED_SQL
    assert payload["row_count"] == 6
    assert [path.rsplit("/", 1)[-1] for path in requests_seen] == [
        "table-list", "table-columns-info", "joinable-tables",
    ]


async def test_missing_host_semantic_client_is_a_config_error(tmp_path):
    """Without a host client the perceptor fails fast instead of using plugin config."""
    payload, _ = await _run_task(
        tmp_path,
        params={
            "perceptor": {"schema_mode": "full_schema"},  # no user_schema, no client
            "semantic": {"base_url": "http://plugin-config.invalid"},  # would feed the legacy path
        },
    )

    assert payload["source"] == "config"
    assert "semantic 服务未配置" in payload["error"]


def test_parent_agent_sees_bound_database_and_routing_without_connection_keys(tmp_path):
    db_path, schema_path = _sqlite_fixture(tmp_path)
    extensions = _compile(_plugin_params(db_path, schema_path), ScriptedModel(responses=[]))

    (child,) = extensions["subagents"]
    assert "db_id=staff, dialect=sqlite, engine=sqlite" in child["description"]
    assert str(db_path) not in child["description"]
    # how to use the subagent lives in its description; the root prompt only
    # restricts the parent's other tools, scoped to the configured database
    assert "result_file" in child["description"] and "one SQL query" in child["description"]
    assert "fetches data only" in child["description"]
    assert "do not translate them" in child["description"]
    assert "only way to query the configured database" in extensions["system_prompt"]
    assert "write_todos" not in extensions["system_prompt"]
    assert "rows_note" not in extensions["system_prompt"]


def test_legacy_yaml_sections_rejected_with_migration_hint():
    from nl2sql.config_manager import NL2SQLConfig

    with pytest.raises(ValueError, match="plugins.config.nl2sql"):
        NL2SQLConfig({"MODEL": {}, "CORE": {}, "model": "primary", "database": {"dialect": "sqlite"}})


def test_result_files_switch_must_be_boolean():
    from nl2sql.config_manager import NL2SQLConfig

    with pytest.raises(ValueError, match="result_files"):
        NL2SQLConfig({"model": "primary", "database": {"dialect": "sqlite"}, "result_files": "yes"})


def test_node_sections_default_to_full_chain_and_require_prefix():
    from nl2sql.config_manager import NL2SQLConfig

    base = {"model": "primary", "database": {"dialect": "sqlite", "path": "/tmp/x.db"}}
    full_chain = ["perceptor", "generator", "validator", "reflector", "executor", "selector"]
    assert NL2SQLConfig(base).enabled_nodes == full_chain
    assert NL2SQLConfig({**base, "perceptor": {}, "generator": {}}).enabled_nodes == ["perceptor", "generator"]
    # the node-chain checks are lazy: they fire when the graph is assembled
    with pytest.raises(ValueError, match="prefix"):
        NL2SQLConfig({**base, "generator": {}, "executor": {}}).enabled_nodes
    with pytest.raises(ValueError, match="generator"):
        NL2SQLConfig({**base, "perceptor": {}}).enabled_nodes


def test_connection_config_strips_reserved_database_keys():
    from nl2sql.config_manager import NL2SQLConfig

    config = NL2SQLConfig({
        "model": "primary",
        "database": {"db_id": "staff", "dialect": "sqlite", "engine": "sqlite", "path": "/tmp/staff.db"},
    })
    assert config.connection_config() == {"path": "/tmp/staff.db"}


# --------------------------------------------------------------------------- helpers


async def _run_task(tmp_path, *, params=None, plugin_responses=None, semantic_client=None, outputs_dir=None):
    """Run one delegated task through the full pipeline; return (reply payload, plugin model)."""
    db_path, schema_path = _sqlite_fixture(tmp_path)
    merged = {**_plugin_params(db_path, schema_path), **(params or {})}
    plugin_model = ScriptedModel(responses=list(plugin_responses or _PLUGIN_RESPONSES))
    parent_model = ScriptedModel(responses=[
        call("task", {"subagent_type": "nl2sql", "description": "每个部门的员工人数"}),
        AIMessage(content="按部门统计的员工人数已经给出。"),
    ])
    graph = _build_agent(
        merged, parent_model, plugin_model, semantic_client=semantic_client, outputs_dir=outputs_dir,
    )
    result = await graph.ainvoke({"messages": [HumanMessage(content="每个部门的员工人数")]})

    reply = next(message for message in result["messages"] if isinstance(message, ToolMessage))
    payload = json.loads(reply.content)
    assert isinstance(result["messages"][-1], AIMessage) and result["messages"][-1].content
    return payload, plugin_model


def _build_agent(params, parent_model, plugin_model, *, semantic_client=None, outputs_dir=None):
    extensions = _compile(params, plugin_model, semantic_client=semantic_client, outputs_dir=outputs_dir)
    assert [child["name"] for child in extensions["subagents"]] == ["nl2sql"]
    assert "runnable" in extensions["subagents"][0]
    return create_deep_agent(model=parent_model, **extensions)


def _compile(params, plugin_model, *, semantic_client=None, outputs_dir=None):
    settings = Settings.model_validate({
        "models": {"primary": {
            "name": "test-model", "base_url": "https://example.invalid/v1", "api_key": "test-secret",
        }},
        "plugins": {"enabled": ["nl2sql"], "config": {"nl2sql": params}},
    })
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
