# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""DataOps migration: native boundaries, execution lifecycle, quality checks and reuse."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/de-agent"


def load(entry, loader=None):
    function = (loader or PythonLoader()).load(PLUGIN, entry)
    return function, sys.modules[function.__module__]


def runtime(tmp_path, **config):
    return SimpleNamespace(
        context=SimpleNamespace(
            session=SimpleNamespace(outputs=tmp_path),
            get_plugin_config=lambda _: {"dataops": config},
        ),
        state={"messages": [HumanMessage(content="requirement", id="turn1")]},
    )


def artifacts(tmp_path):
    ddl, dml = tmp_path / "create_t.sql", tmp_path / "insert_t.sql"
    ddl.write_text("CREATE TABLE db.t (id STRING)")
    dml.write_text("INSERT OVERWRITE TABLE db.t SELECT id FROM db.src")
    return ddl, dml


def checker_validation(tmp_path):
    cls, module = load("utils/sql_checkers.py:SQLCheckerPipeline")

    async def validate(ddl, dml, context, query):
        pipeline = cls(tmp_path, None, PLUGIN / "skills/sql-rules/references", context=context)
        pipeline.report_dir.mkdir(parents=True)
        await pipeline._validate_dataops(
            {"ddl": ddl.read_text(), "dml": dml.read_text()}, {"ddl": ddl, "dml": dml}
        )
        return pipeline.report["dataops"]

    return validate, module.dataops_validate_tool


@pytest.mark.parametrize("failure", [None, "ddl", "dml", "skipped"])
async def test_delivery_orders_sql_and_does_not_treat_skipped_as_passed(
    tmp_path, monkeypatch, failure
):
    function, module = checker_validation(tmp_path)
    ddl, dml = artifacts(tmp_path)
    calls = []

    async def validate(sql, *, attempt, prior_attempts, context, _enable_thinking=False):
        kind = "ddl" if sql.startswith("CREATE") else "dml"
        calls.append((kind, attempt))
        return {"passed": kind != failure, "skipped": failure == "skipped", "error": "fix column"}

    monkeypatch.setattr(module, "dataops_validate_sql_with_log_analysis", validate)
    result = await function(ddl, dml, runtime(tmp_path).context, "requirement")
    assert calls == ([("ddl", 1)] if failure in ("ddl", "skipped") else [("ddl", 1), ("dml", 1)])
    assert result["status"] == ("PASS" if failure is None else "BLOCKED")


async def test_budget_survives_sql_changes_and_is_scoped_to_file_pair(tmp_path, monkeypatch):
    function, module = checker_validation(tmp_path)
    ddl, dml = artifacts(tmp_path)
    remote = AsyncMock(return_value={"passed": False, "error": "bad ddl"})
    monkeypatch.setattr(module, "dataops_validate_sql_with_log_analysis", remote)
    for index in range(11):
        ddl.write_text(f"CREATE TABLE db.t (id STRING) -- {index}")
        result = await function(ddl, dml, runtime(tmp_path).context, "q")
    assert remote.await_count == 10
    assert [c.kwargs["attempt"] for c in remote.await_args_list] == list(range(1, 11))
    assert result["reason"] == "attempts_exhausted"
    other = tmp_path / "create_other.sql"
    other.write_text(ddl.read_text())
    await function(other, dml, runtime(tmp_path).context, "q")
    assert remote.await_count == 11


async def test_successful_unchanged_sql_reused_and_cancellation_reserves_attempt(
    tmp_path, monkeypatch
):
    function, module = checker_validation(tmp_path)
    ddl, dml = artifacts(tmp_path)
    remote = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(module, "dataops_validate_sql_with_log_analysis", remote)
    with pytest.raises(asyncio.CancelledError):
        await function(ddl, dml, runtime(tmp_path).context, "q")
    assert not list((tmp_path / ".runtime/sql_checkers").glob("*.lock"))
    remote.side_effect = None
    remote.return_value = {"passed": True}
    await function(ddl, dml, runtime(tmp_path).context, "q")
    assert remote.await_args_list[1].kwargs["attempt"] == 2
    result = await function(ddl, dml, runtime(tmp_path).context, "q")
    assert result["reused"] is True
    assert remote.await_count == 3


async def test_integration_protocol_async_poll_and_collect(tmp_path, monkeypatch):
    loader = PythonLoader()
    cls, module = load("utils/dataops_mcp.py:IntegrationClient", loader)
    remote = AsyncMock(
        side_effect=[
            {"success": True, "data": {"job_id": "123", "status": "queued"}},
            {"success": True, "data": {"status": "completed"}},
            {"success": True, "data": {"status": "completed", "data": [{"id": 1}]}},
        ]
    )
    monkeypatch.setattr(module, "call_mcp", remote)
    client = cls({"integration_url": "http://localhost/mcp"})
    assert (
        await client.submit_job(
            resource_id="dataops",
            command="SELECT id FROM t",
            task_type="sql_validate",
            timeout_sec=60,
        )
    )["job_id"] == "123"
    poll, _ = load("utils/dataops_validate_tool.py:_poll_until_done", loader)
    status, result = await poll(client, "123", 60, 0)
    assert status == "completed" and result["data"] == [{"id": 1}]
    assert [call.args[0] for call in remote.await_args_list] == [
        "submit_job",
        "poll_job",
        "collect_job",
    ]


async def test_context_settings_isolate_concurrent_invocations(tmp_path):
    scope, module = load("utils/dataops_mcp.py:dataops_settings")

    async def task(environment):
        with scope(runtime(tmp_path, environment=environment).context):
            await asyncio.sleep(0)
            return module.getenv("DATAOPS_ENV")

    assert await asyncio.gather(task("prod"), task("integration")) == ["prod", "integration"]
    assert module.settings.get() is None


async def test_cte_timeout_uses_async_model_and_falls_back(monkeypatch):
    function, module = load("utils/dataops_validate_tool.py:_expand_cte_for_dml")
    model = SimpleNamespace(
        ainvoke=AsyncMock(return_value=SimpleNamespace(content="INSERT INTO t SELECT id FROM src"))
    )
    original = "WITH s AS (SELECT id FROM src) INSERT INTO t SELECT id FROM s"
    assert (
        await function(
            original,
            context=SimpleNamespace(get_model=lambda _: model, get_plugin_config=lambda _: {}),
        )
        == "INSERT INTO t SELECT id FROM src"
    )
    original_wait = asyncio.wait_for

    async def timeout(coro, timeout):
        assert timeout == 1
        coro.close()
        raise TimeoutError

    monkeypatch.setattr(module.asyncio, "wait_for", timeout)
    assert (
        await function(
            original,
            timeout_sec=1,
            context=SimpleNamespace(get_model=lambda _: model, get_plugin_config=lambda _: {}),
        )
        == original
    )
    monkeypatch.setattr(module.asyncio, "wait_for", original_wait)


async def test_trial_creates_checks_and_cleans_with_same_partition(tmp_path, monkeypatch):
    function, module = load("utils/dataops_trial_compute_tool.py:trial_compute_table")
    ddl, dml = artifacts(tmp_path)
    executed = AsyncMock(return_value={"passed": True, "job_id": "123"})
    quality = AsyncMock(return_value={"ok": False, "warnings": ["low coverage"]})
    cleanup = AsyncMock()
    monkeypatch.setattr(module, "dataops_validate_sql", executed)
    monkeypatch.setattr(module, "run_quality_check", quality)
    monkeypatch.setattr(module, "_drop_scratch_table", cleanup)
    monkeypatch.setattr(module, "partition_date_for_quality", lambda: "20261023")
    context = runtime(tmp_path).context
    result = await function(dml_path=str(dml), context=context)
    assert result["passed"] is True  # quality warnings remain non-blocking
    assert executed.await_count == 2
    assert all(c.kwargs["_partition_date"] == "20261023" for c in executed.await_args_list)
    assert executed.await_args_list[1].kwargs["_collapse_partition_range"] is True
    assert quality.await_args.kwargs["_partition_date"] == "20261023"
    assert cleanup.await_args.args[2] == "20261023"


async def test_trial_cleans_after_insert_failure(tmp_path, monkeypatch):
    function, module = load("utils/dataops_trial_compute_tool.py:trial_compute_table")
    _, dml = artifacts(tmp_path)
    monkeypatch.setattr(
        module,
        "dataops_validate_sql",
        AsyncMock(side_effect=[{"passed": True}, RuntimeError("offline")]),
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(module, "_drop_scratch_table", cleanup)
    with pytest.raises(RuntimeError):
        await function(
            dml_path=str(dml),
            context=runtime(tmp_path).context,
        )
    cleanup.assert_awaited_once()


async def test_manual_trial_and_hook_share_result_and_preserve_message(tmp_path, monkeypatch):
    loader = PythonLoader()
    tool, _ = load("tools/dataops.py:trial_compute_table", loader)
    hook, _ = load("hooks/dataops_quality_check_post.py:dataops_quality_check_post", loader)
    core = tool.__globals__["_trial"].__globals__
    executed = AsyncMock(
        return_value={
            "passed": True,
            "quality_check": {"ok": True, "sampled": True, "sample_size": 10},
        }
    )
    monkeypatch.setitem(core, "trial_compute_table", executed)
    _, dml = artifacts(tmp_path)
    rt = runtime(tmp_path, integration_url="http://localhost/mcp")
    rt.state["messages"].append(AIMessage(content="delivery", id="answer"))
    await tool(rt, dml_path=str(dml))
    assert await hook(rt.state, rt, params={}) is None
    executed.assert_awaited_once()
    dml.write_text(dml.read_text() + " -- changed")
    update = await hook(rt.state, rt, params={})
    assert update["messages"][0].id != "answer"
    assert rt.state["messages"][-1].content == "delivery"
    assert "delivery" in update["messages"][0].content
    assert "数据正确性校验全部通过" in update["messages"][0].content
    assert executed.await_count == 2


async def test_real_table_validation_only_calls_quality_checker(tmp_path, monkeypatch):
    function, module = load("utils/dataops_validate_data_tool.py:validate_data")
    quality = AsyncMock(return_value={"ok": True, "sampled": True, "warnings": [], "details": {}})
    monkeypatch.setattr(module, "run_quality_check", quality)
    monkeypatch.setattr(
        module, "fetch_semantic_columns", lambda *a, **k: (["user_id"], {"user_id": "string"})
    )
    result = await function(target_table="db.t", context=runtime(tmp_path).context)
    assert result["data"]["target_table"] == "db.t"
    assert quality.await_args.kwargs["direct_table"] == "db.t"


async def test_probe_validation_and_disabled_tools(tmp_path):
    function, _ = load("tools/dataops.py:dataops_probe_table")
    assert (await function("db.t", runtime(tmp_path), columns=[]))["reason"] == "invalid_columns"
    assert (
        await function("db.t", runtime(tmp_path), columns=["id"], partition_filter="pt_d='$date'")
    )["reason"] == "invalid_partition_filter"
    assert (await function("db.t", runtime(tmp_path, enabled=False), columns=["id"]))[
        "reason"
    ] == "dataops_disabled"


def test_quality_config_and_three_registered_checkers(tmp_path):
    resolve, _ = load("utils/dataops_quality_check.py:resolve_config")
    config = resolve(
        runtime(
            tmp_path,
            quality_check={
                "sample_size": 42,
                "checks": ["null_rate", "single_value_ratio", "outlier_ratio"],
            },
        ).context
    )
    assert config.sample_size == 42
    assert [c.name for c in config.checkers] == ["null_rate", "single_value_ratio", "outlier_ratio"]


async def test_static_failure_never_executes_dataops(tmp_path, monkeypatch):
    function, module = load("tools/sql_checkers.py:check_and_repair_sql")
    ddl, dml = artifacts(tmp_path)
    remote = AsyncMock()
    monkeypatch.setattr(module.SQLCheckerPipeline, "_validate_dataops", remote)
    rt = runtime(tmp_path)
    result = await function(str(ddl), str(dml), rt, repair=False)
    assert result["status"] == "BLOCKED"
    remote.assert_not_awaited()


async def test_quality_checks_run_concurrently_and_preserve_partial_metrics(tmp_path, monkeypatch):
    function, module = load("utils/dataops_quality_check.py:run_quality_check")
    monkeypatch.setattr(module, "_get_dataops_env", lambda: "prod")
    started = 0
    all_started = asyncio.Event()

    async def execute(sql, timeout):
        nonlocal started
        if "COUNT(" not in sql.upper() and "SUM(" not in sql.upper():
            return "completed", "sample", {"data": []}
        index = started
        started += 1
        if started == 4:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), 1)
        if index == 1:
            raise RuntimeError("single-value query offline")
        return "completed", str(index), {"data": [{"score_ratio": 0.1}]}

    monkeypatch.setattr(module, "_execute_check_sql_prod", execute)
    result = await function(
        "INSERT INTO db.t SELECT score_ratio FROM db.src",
        direct_table="db.t",
        context=runtime(tmp_path).context,
    )
    assert started == 4
    assert result["sampled"] is True and result["partial"] is True
    assert result["ok"] is False
    assert result["completed_checks"] == ["null_rate", "outlier_ratio", "row_count"]
    assert result["failed_checks"][0]["name"] == "single_value_ratio"
    assert result["details"]["score_ratio"]["null_rate"] == 0.1
    assert result["warnings"]


async def test_native_after_agent_hook_appends_report_without_losing_delivery(
    tmp_path, monkeypatch
):
    from conftest import ScriptedModel

    from dataagent.config.declarations import HookSpec
    from dataagent.core.graph import create_deep_agent
    from dataagent.extensions.hooks import compile_hook

    loader = PythonLoader()
    hook, module = load("hooks/dataops_quality_check_post.py:dataops_quality_check_post", loader)
    artifacts(tmp_path)
    monkeypatch.setattr(
        module,
        "trial_compute_once",
        AsyncMock(
            return_value=(
                {"passed": True, "quality_check": {"ok": True, "sampled": True, "sample_size": 10}},
                False,
            )
        ),
    )
    graph = create_deep_agent(
        model=ScriptedModel(responses=[AIMessage(content="delivery", id="answer")]),
        context_schema=SimpleNamespace,
        middleware=[
            compile_hook(
                HookSpec(
                    event="after_agent",
                    entrypoint="hooks/dataops_quality_check_post.py:dataops_quality_check_post",
                ),
                hook,
                name="DataOpsAfter",
            )
        ],
    )
    events = [
        event
        async for event in graph.astream_events(
            {"messages": [HumanMessage(content="q", id="user")]},
            context=runtime(tmp_path).context,
            version="v2",
        )
    ]
    emitted = [
        event["data"]
        for event in events
        if event["event"] == "on_custom_event" and event["name"] == "manually_emit_message"
    ]
    assert len(emitted) == 2
    assert "请稍候" in emitted[0]["message"]
    assert "delivery" not in emitted[1]["message"]
    result = events[-1]["data"]["output"]
    answers = [message for message in result["messages"] if isinstance(message, AIMessage)]
    assert len(answers) == 2
    assert answers[0].content == "delivery"
    assert "delivery" in answers[1].content
    assert "数据正确性校验全部通过" in answers[1].content
    assert emitted[1]["message_id"] == answers[1].id


@pytest.mark.parametrize(
    "status,expected", [("Running", "running"), ("Success", "completed"), ("Failed", "failed")]
)
async def test_production_status_mapping_and_error_summary(status, expected, monkeypatch):
    function, module = load("utils/dataops_prod_adapter.py:prod_get_query_result")
    monkeypatch.setattr(
        module,
        "_call_official_mcp",
        AsyncMock(return_value={"success": True, "data": {"status": status}}),
    )
    assert (await function("123"))["status"] == expected
    assert len(module._summarize_error_log("x" * 10000)) < 600


@pytest.mark.parametrize("partition_date", ["20261023", "20271030"])
def test_rewrite_and_cleanup_share_explicit_partition_date(partition_date):
    function, module = load("utils/dataops_validate_tool.py:_replace_target_table")
    sql = "INSERT OVERWRITE TABLE db.t PARTITION (pt_d='$date') SELECT user_id FROM db.src WHERE pt_d='$date'"
    rewritten = function(sql, "alice", "run1", partition_date)
    (table,) = module._extract_temp_table_names(sql, "alice", "run1", partition_date)
    assert table in rewritten and partition_date in table
    assert "$date" not in rewritten
    assert partition_date in rewritten
    assert "$date" in sql  # artifact input is not changed


async def test_missing_integration_service_does_not_report_trial_passed(tmp_path):
    function, _ = load("hooks/dataops_quality_check_post.py:dataops_quality_check_post")
    artifacts(tmp_path)
    rt = runtime(tmp_path, environment="integration")
    rt.state["messages"].append(AIMessage(content="delivery", id="answer"))
    update = await function(rt.state, rt, params={})
    assert "已跳过" in update["messages"][0].content
    assert "试算通过" not in update["messages"][0].content


async def test_tool_passes_real_model_context_unchanged(tmp_path, monkeypatch):
    from dataagent import LaunchOptions, prepare_runtime
    from dataagent.assembly.context import AgentContext

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": "unused",
                        "base_url": "http://localhost/v1",
                        "api_key": "unused",
                    }
                },
                "plugins": {
                    "enabled": ["de-agent"],
                    "config": {
                        "de-agent": {"dataops": {"environment": "prod", "exec_user": "alice"}}
                    },
                },
            }
        )
    )
    host = prepare_runtime(LaunchOptions(cwd=tmp_path, config=config_path))
    context = AgentContext(host, session=host.paths.for_session("native-dataops"))
    function, module = load("tools/dataops.py:dataops_probe_table")
    seen = []

    async def probe(table, columns, *, context: AgentContext, **kwargs):
        assert isinstance(context, AgentContext)
        seen.append(context)
        assert module.dataops_options(context)["exec_user"] == "alice"
        return {"ok": True}

    monkeypatch.setattr(module, "_probe", probe)
    result = await function("db.t", SimpleNamespace(context=context), columns=["id"])
    assert result["ok"] and seen == [context]
