"""Regressions for !596 cleanup ownership and !617 budgets/report delivery."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/de-agent"


def load(entry, loader=None):
    function = (loader or PythonLoader()).load(PLUGIN, entry)
    return function, sys.modules[function.__module__]


def context(tmp_path, **config):
    return SimpleNamespace(
        session=SimpleNamespace(outputs=tmp_path),
        get_plugin_config=lambda _: {"dataops": config},
    )


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT OVERWRITE TABLE `biads`.`ads_x` SELECT id FROM src",
        "-- create table biads.fake\nINSERT OVERWRITE biads.ads_x SELECT 'it\\'s--ok' AS id FROM src",
        "/* CREATE TABLE ignored */ INSERT INTO biads.ads_x SELECT 'a''--b' FROM src",
    ],
)
def test_comment_classification_and_quoted_target_share_cleanup_identity(sql):
    _, module = load("utils/dataops_validate_tool.py:_is_create_sql")
    assert not module._is_create_sql(sql)
    assert module._is_insert_sql(sql)
    names = module._extract_temp_table_names(sql, "alice@corp.com", "run-1", "20271113")
    assert len(names) == 1
    assert names[0].endswith("_ads_x_run_1")
    assert module._is_adhoctemp_temp_table(names[0])


def test_mixed_file_cleans_every_created_or_written_table():
    _, module = load("utils/dataops_validate_tool.py:_extract_temp_table_names")
    sql = "CREATE TABLE db.ads_a (id STRING); INSERT OVERWRITE db.ads_b SELECT id FROM src"
    names = module._extract_temp_table_names(sql, "alice", "", "20271113")
    assert len(names) == 2
    assert {name.rsplit("_", 1)[-1] for name in names} == {"a", "b"}


@pytest.mark.parametrize(
    "name", ["biads.ads_x", "adhoctemp.real_table", "adhoctemp.tmp_x; DROP TABLE src"]
)
async def test_cleanup_never_drops_business_or_invalid_table(name):
    function, _ = load("utils/dataops_validate_tool.py:_drop_one_temp_table")
    client = SimpleNamespace(submit_job=AsyncMock())
    assert await function(client, name, 3600, 30, context_label="test") is None
    client.submit_job.assert_not_awaited()


async def test_drop_poll_has_more_time_and_does_not_cancel_on_timeout(monkeypatch):
    function, module = load("utils/dataops_validate_tool.py:_drop_one_temp_table")
    poll = AsyncMock(return_value=("timed_out", None))
    monkeypatch.setattr(module, "_poll_until_done", poll)
    client = SimpleNamespace(submit_job=AsyncMock(return_value={"job_id": "drop"}))
    assert await function(client, "adhoctemp.tmp_x", 3600, 30, context_label="test") == "drop"
    assert client.submit_job.await_args.kwargs["timeout_sec"] == 300
    assert poll.await_args.kwargs["cancel_on_timeout"] is False


async def test_production_cleanup_uses_the_same_scratch_table_guard(monkeypatch):
    loader = PythonLoader()
    function, prod = load("utils/dataops_validate_tool.py:_drop_one_temp_table_prod", loader)
    execute = AsyncMock(return_value={"success": True, "job_id": "drop"})
    collect = AsyncMock(return_value={})
    monkeypatch.setattr(prod, "prod_execute_sql", execute)
    monkeypatch.setattr(
        prod, "prod_get_query_result", AsyncMock(return_value={"status": "completed"})
    )
    monkeypatch.setattr(prod, "prod_collect_result", collect)
    assert await function("biads.ads_x", 3600, context_label="test") is None
    execute.assert_not_awaited()
    assert await function("adhoctemp.tmp_x", 3600, context_label="test") == "drop"
    execute.assert_awaited_once_with("DROP TABLE IF EXISTS adhoctemp.tmp_x")
    collect.assert_awaited_once_with("drop")


@pytest.mark.parametrize("cancel", [True, False])
async def test_timeout_cancellation_policy(cancel):
    function, _ = load("utils/dataops_validate_tool.py:_poll_until_done")
    client = SimpleNamespace(cancel=AsyncMock())
    assert await function(client, "job", 0, 30, cancel_on_timeout=cancel) == ("timed_out", None)
    assert client.cancel.await_count == int(cancel)


@pytest.mark.parametrize("kind", ["dml", "ddl", "mixed"])
@pytest.mark.parametrize("passed", [True, False])
async def test_wrapper_cleans_dml_on_every_outcome_but_preserves_create_for_insert(
    tmp_path,
    monkeypatch,
    kind,
    passed,
):
    function, module = load("utils/dataops_validate_tool.py:dataops_validate_sql_with_log_analysis")
    dml = "INSERT OVERWRITE db.ads_x SELECT id FROM src"
    ddl = "CREATE TABLE db.ads_x (id STRING)"
    sql = {"ddl": ddl, "dml": dml, "mixed": ddl + ";" + dml}[kind]
    monkeypatch.setattr(
        module, "dataops_validate_sql", AsyncMock(return_value={"passed": passed, "error": "bad"})
    )
    cleanup = AsyncMock(return_value="drop")
    monkeypatch.setattr(module, "_cleanup_temp_table_wrapper", cleanup)
    result = await function(sql, context=context(tmp_path))
    assert result["passed"] is passed
    assert cleanup.await_count == int(kind == "dml")


@pytest.mark.parametrize("failure", ["ddl", "dml", None])
async def test_round_cleanup_runs_after_validation_without_replacing_verdict(
    tmp_path,
    monkeypatch,
    failure,
):
    loader = PythonLoader()
    cls, checker = load("utils/sql_checkers.py:SQLCheckerPipeline", loader)
    events = []
    ddl, dml = tmp_path / "create.sql", tmp_path / "insert.sql"
    ddl.write_text("CREATE TABLE db.ads_x (id STRING)")
    dml.write_text("INSERT OVERWRITE db.ads_x SELECT id FROM src")

    async def validate(sql=None, *, sql_file_path=None, **kwargs):
        text = Path(sql_file_path).read_text() if sql_file_path else sql
        kind = "ddl" if text.startswith("CREATE") else "dml"
        events.append(kind)
        return {"passed": failure != kind, "error": "bad"}

    async def cleanup(sqls, **kwargs):
        events.append("cleanup")
        assert len(sqls) == 2
        raise RuntimeError("cleanup unavailable")

    monkeypatch.setattr(
        checker.dataops_validate_tool, "dataops_validate_sql_with_log_analysis", validate
    )
    monkeypatch.setattr(checker.dataops_validate_tool, "cleanup_temp_tables", cleanup)
    ctx = context(tmp_path)
    pipeline = cls(tmp_path, None, PLUGIN / "skills/sql-rules/references", context=ctx)
    pipeline.report_dir.mkdir(parents=True)
    passed = await pipeline._validate_dataops(
        {"ddl": ddl.read_text(), "dml": dml.read_text()},
        {"ddl": ddl, "dml": dml},
    )
    assert passed is (failure is None)
    assert events == (["ddl", "cleanup"] if failure == "ddl" else ["ddl", "dml", "cleanup"])
    assert not list(tmp_path.rglob("*.lock"))


async def test_trial_cleanup_preserves_cancellation(tmp_path, monkeypatch):
    function, module = load("utils/dataops_trial_compute_tool.py:_drop_scratch_table")
    monkeypatch.setattr(
        module, "cleanup_temp_tables", AsyncMock(side_effect=asyncio.CancelledError)
    )
    with pytest.raises(asyncio.CancelledError):
        await function(["INSERT INTO db.x SELECT id FROM src"], context(tmp_path), "20271113", {})


@pytest.mark.parametrize("columns", [None, [], ["db.t.id"], ["*"], "id"])
async def test_probe_rejects_missing_and_qualified_columns_before_transport(tmp_path, columns):
    function, _ = load("utils/dataops_probe_tool.py:dataops_probe_table")
    result = await function("db.t", columns, context=context(tmp_path))
    assert result["reason"] == "invalid_columns"


def test_probe_schema_requires_columns_and_exposes_its_constraints():
    function, _ = load("tools/dataops.py:dataops_probe_table")
    schema = tool(function).tool_call_schema.model_json_schema()
    assert "columns" in schema["required"]
    assert "单段" in schema["properties"]["columns"]["description"]


def test_budget_constant_and_exhaustion_diagnostics():
    loader = PythonLoader()
    _, checker = load("utils/sql_checkers.py:SQLCheckerPipeline", loader)
    assert (
        checker.DEFAULT_MAX_DATAOPS_VALIDATION_ATTEMPTS
        == checker.dataops_validate_tool.MAX_VALIDATE_ATTEMPTS
        == 10
    )
    report = checker._describe_attempt_budget(
        exhausted_file="dml",
        max_attempts=10,
        attempts_used={"ddl": 10, "dml": 10},
        attempt_history={"dml": [{"attempt": 10, "status": "BLOCKED", "detail": "分区字段不存在"}]},
    )
    assert report["rejected_attempt"] == 11
    assert "分区字段不存在" in report["explanation"]
    pipeline = object.__new__(checker.SQLCheckerPipeline)
    pipeline.report = {"dataops": report}
    summary = pipeline._dataops_failure_summary()
    assert "不要直接重复调用 checker" in summary
    assert "分区字段不存在" in summary
    assert "utils.dataops_constants" in summary


async def test_report_stream_keeps_delivery_and_progress_out_of_history(tmp_path, monkeypatch):
    function, module = load("hooks/dataops_quality_check_post.py:dataops_quality_check_post")
    (tmp_path / "insert_x.sql").write_text("INSERT INTO db.x SELECT id FROM src")
    monkeypatch.setattr(
        module,
        "trial_compute_once",
        AsyncMock(
            return_value=(
                {"passed": True, "quality_check": {"sampled": True, "ok": True}},
                False,
            )
        ),
    )
    original = AIMessage(content="delivery", id="answer")
    state = {"messages": [HumanMessage(content="q"), original]}
    events = []
    runtime = SimpleNamespace(context=context(tmp_path))

    async def emit(name, data):
        assert name == "manually_emit_message"
        events.append(data)

    monkeypatch.setattr(module, "adispatch_custom_event", emit)
    update = await function(state, runtime, params={})
    assert original.content == "delivery"
    assert "## 最终交付" in update["messages"][0].content
    assert len(events) == 2
    assert "请稍候" in events[0]["message"]
    assert "delivery" not in events[1]["message"]
    assert "请稍候" not in json.dumps(update["messages"][0].content, ensure_ascii=False)
