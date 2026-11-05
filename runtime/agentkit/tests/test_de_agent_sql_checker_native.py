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
"""Native model, session evidence and candidate publication boundaries."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "builtin-plugins/de-agent"
DDL = """CREATE EXTERNAL TABLE IF NOT EXISTS biads.ads_result (
user_id STRING COMMENT '用户', cnt BIGINT COMMENT '计数'
) COMMENT '结果表' PARTITIONED BY (pt_d STRING COMMENT '天分区') STORED AS ORC"""
DML = "INSERT OVERWRITE biads.ads_result PARTITION (pt_d='$date') SELECT user_id, COUNT(*) AS cnt FROM src GROUP BY user_id"


def setup(tmp_path, *, config=None, model=None):
    if model is not None:
        async def stream(messages, **kwargs):
            yield await model.ainvoke(messages, **kwargs)
        model.astream = stream
    loader = PythonLoader()
    tool = loader.load(PLUGIN, "tools/sql_checkers.py:check_and_repair_sql")
    module = sys.modules[tool.__module__]
    context = SimpleNamespace(
        session=SimpleNamespace(outputs=tmp_path),
        get_plugin_config=lambda _: config or {"dataops": {"enabled": False}},
        get_model=lambda alias: model,
    )
    for kind, content in (("ddl", DDL), ("dml", DML)):
        (tmp_path / f"{kind}.sql").write_text(content)
    return tool, module, SimpleNamespace(context=context), loader


def repairing_model():
    async def repair(messages):
        payload = json.loads(messages[1]["content"])
        return AIMessage(
            content=json.dumps(
                {
                    "issue_id": payload["issue_id"],
                    "edits": [
                        {"file": "dml", "old_text": "INSERT INTO", "new_text": "INSERT OVERWRITE"}
                    ],
                }
            )
        )

    return SimpleNamespace(ainvoke=AsyncMock(side_effect=repair))


async def test_native_model_repairs_then_publishes_with_audit_files(tmp_path):
    model = repairing_model()
    tool, _, runtime, _ = setup(tmp_path, model=model)
    (tmp_path / "dml.sql").write_text(DML.replace("OVERWRITE", "INTO"))
    result = await tool("ddl.sql", "dml.sql", runtime)
    assert result["status"] == "PASS" and result["passed"] is True
    assert result["model_calls"] == 1
    assert (tmp_path / "dml.sql").read_text() == DML
    report_dir = Path(result["report_path"]).parent
    assert list(report_dir.glob("*_request.json")) and list(report_dir.glob("*.diff"))
    assert result["dataops"]["status"] == "DISABLED"
    assert len(result["changed_files"]) == 1


async def test_repair_false_never_gets_model(tmp_path):
    tool, _, runtime, _ = setup(tmp_path)
    runtime.context.get_model = lambda _: pytest.fail("Detection must not get a model")
    (tmp_path / "dml.sql").write_text(DML.replace("OVERWRITE", "INTO"))
    result = await tool("ddl.sql", "dml.sql", runtime, repair=False)
    assert result["status"] == "BLOCKED" and result["model_calls"] == 0


async def test_budget_rejects_invalid_model_patch_and_preserves_files(tmp_path):
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="not JSON")))
    tool, _, runtime, _ = setup(
        tmp_path,
        model=model,
        config={
            "sql_checkers": {"max_model_calls": 1},
            "dataops": {"enabled": False},
        },
    )
    invalid = DML.replace("OVERWRITE", "INTO")
    (tmp_path / "dml.sql").write_text(invalid)
    result = await tool("ddl.sql", "dml.sql", runtime)
    assert result["status"] == "BLOCKED" and model.ainvoke.await_count == 1
    assert (tmp_path / "dml.sql").read_text() == invalid


async def test_dataops_receives_repaired_candidate_but_failure_does_not_publish(
    tmp_path, monkeypatch
):
    model = repairing_model()
    tool, module, runtime, _ = setup(tmp_path, model=model, config={"dataops": {"enabled": True}})
    dataops = module.SQLCheckerPipeline._validate_dataops.__globals__["dataops_validate_tool"]
    remote = AsyncMock(
        side_effect=[{"passed": True}, {"passed": False, "error": "DML compile failed"}]
    )
    monkeypatch.setattr(dataops, "dataops_validate_sql_with_log_analysis", remote)
    original = DML.replace("OVERWRITE", "INTO")
    (tmp_path / "dml.sql").write_text(original)
    result = await tool("ddl.sql", "dml.sql", runtime)
    assert result["status"] == "BLOCKED" and result["changed_files"] == []
    assert remote.await_args_list[0].args[0] == DDL
    assert remote.await_args_list[1].args[0] == DML
    assert remote.await_args.kwargs["context"] is runtime.context
    assert (tmp_path / "dml.sql").read_text() == original
    assert (Path(result["report_path"]).parent / "candidate_dml.sql").read_text() == DML


async def test_sql_checker_specific_switch_can_disable_dataops(tmp_path):
    tool, _, runtime, _ = setup(
        tmp_path,
        config={
            "sql_checkers": {"dataops_validation_enabled": False},
            "dataops": {"enabled": True},
        },
    )
    result = await tool("ddl.sql", "dml.sql", runtime, repair=False)
    assert result["status"] == "PASS" and result["dataops"]["status"] == "DISABLED"


async def test_tool_rejects_paths_outside_session(tmp_path):
    tool, _, runtime, _ = setup(tmp_path)
    result = await tool("../ddl.sql", "dml.sql", runtime)
    assert result["status"] == "ERROR" and result["passed"] is False


async def test_native_schema_evidence_keeps_latest_types_and_provenance(tmp_path):
    _, _, runtime, loader = setup(tmp_path)
    save = loader.load(PLUGIN, "utils/evidence.py:save_evidence")
    load_schema = loader.load(PLUGIN, "utils/sql_query_checks.py:load_schema_evidence")
    save(runtime.context, "schema", "db.src", {"db.src.id": {"value_type": "STRING"}})
    latest = save(
        runtime.context,
        "schema",
        "db.src",
        {"db.src.id": {"type": "BIGINT", "description": "用户"}},
    )
    tables, evidence = load_schema(tmp_path)
    column = tables["db.src"]["columns"]["id"]
    assert column["value_type"] == "BIGINT" and column["description"] == "用户"
    assert Path(latest["evidence_path"]).name in column["source"]
    assert evidence["status"] == "loaded" and evidence["table_count"] == 1


async def test_native_join_evidence_reaches_static_semantics(tmp_path):
    _, _, runtime, loader = setup(tmp_path)
    save = loader.load(PLUGIN, "utils/evidence.py:save_evidence")
    check = loader.load(PLUGIN, "utils/delivery_contract.py:check_static_semantics")
    save(
        runtime.context,
        "joins",
        ["db.fact", "db.dim"],
        [
            {"src": "db.fact.id", "target_column": ["db.dim.id"], "rel_type": "LEFT"},
        ],
    )
    issues = check(DDL, "SELECT f.id FROM db.fact f INNER JOIN db.dim d ON f.id=d.id", tmp_path)
    assert issues[0]["code"] == "SQL_JOIN_TYPE_MISMATCH"


async def test_provider_error_does_not_publish_or_leak_error_text(tmp_path):
    model = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("provider-secret")))
    tool, _, runtime, _ = setup(tmp_path, model=model)
    invalid = DML.replace("OVERWRITE", "INTO")
    (tmp_path / "dml.sql").write_text(invalid)
    result = await tool("ddl.sql", "dml.sql", runtime)
    assert result["status"] == "ERROR"
    assert "provider-secret" not in Path(result["report_path"]).read_text()
    assert (tmp_path / "dml.sql").read_text() == invalid
