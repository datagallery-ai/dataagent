"""Check program-owned patch boundaries; real LLM behavior is exercised end to end."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.cbb.agent_env import Env
from dataagent.core.cbb.runtime import Runtime
from dataagent.core.suite.builtin_suites.de_agent.tools.sql_checkers import SQLCheckerPipeline
from dataagent.core.suite.builtin_suites.de_agent.tools.sql_rule_detectors import RuleDetector

RULES = Path(__file__).resolve().parents[3] / "dataagent/core/suite/builtin_suites/de_agent/skill/sql_rules/references"
DDL = """CREATE EXTERNAL TABLE IF NOT EXISTS biads.ads_result (
user_id STRING COMMENT '用户', cnt BIGINT COMMENT '计数'
) COMMENT '结果表' PARTITIONED BY (pt_d STRING COMMENT '天分区') STORED AS ORC"""
DML = (
    "INSERT OVERWRITE biads.ads_result PARTITION (pt_d='$date') "
    "SELECT user_id, COUNT(*) AS cnt FROM src GROUP BY user_id"
)


def test_supported_artifact_pair_and_column_order():
    """Detect a same-name column swap and report unverified positional aliases without guessing a patch."""
    detector = RuleDetector(RULES)
    assert detector.detect({"ddl": DDL, "dml": DML}) == []
    wrong = DML.replace("user_id, COUNT(*) AS cnt", "COUNT(*) AS cnt, user_id")
    assert any(item.rule_id == "PROJECTION_ALIGNMENT" for item in detector.detect({"ddl": DDL, "dml": wrong}))
    renamed = DML.replace("COUNT(*) AS cnt", "COUNT(*) AS total")
    issues = detector.detect({"ddl": DDL, "dml": renamed})
    assert any(item.rule_id == "PROJECTION_UNVERIFIED" and item.replacement is None for item in issues)
    assert all("severity" not in item.to_dict() for item in issues)


@pytest.mark.parametrize("replacement", ["DROP TABLE biads.ads_result", "INSERT OVERWRITE; DROP TABLE src"])
def test_repair_cannot_introduce_other_statements(replacement):
    """Reject destructive or additional statements in a model-chosen patch."""
    detector = RuleDetector(RULES)
    artifacts = {"ddl": DDL, "dml": DML.replace("INSERT OVERWRITE", "INSERT INTO")}
    issue = next(item for item in detector.detect(artifacts) if item.rule_id == "WRITE_MODE")
    with pytest.raises(ValueError):
        detector.apply(artifacts, issue, _patch(issue, "dml", artifacts.get("dml"), replacement))


def test_repair_is_local_and_stale_locations_are_rejected():
    """Only the located write-mode fragment changes; shifted input invalidates a patch."""
    detector = RuleDetector(RULES)
    artifacts = {"ddl": DDL, "dml": DML.replace("INSERT OVERWRITE", "INSERT INTO")}
    issue = next(item for item in detector.detect(artifacts) if item.rule_id == "WRITE_MODE")
    response = _patch(issue, "dml", "INSERT INTO", "INSERT OVERWRITE")
    assert detector.apply(artifacts, issue, response) == {"ddl": DDL, "dml": DML}
    with pytest.raises(ValueError, match="Stale"):
        detector.apply({"ddl": DDL, "dml": "-- added comment\n" + artifacts.get("dml")}, issue, response)


@pytest.mark.parametrize("sql", ["DROP TABLE biads.ads_result", DML + "; DROP TABLE src", "SELECT 1"])
def test_unverified_input_is_not_a_pass(sql):
    """Unexpected statements remain unverified and cannot receive an automatic patch."""
    issues = RuleDetector(RULES).detect({"ddl": DDL, "dml": sql})
    assert any(item.rule_id == "PARSE_UNVERIFIED" and item.replacement is None for item in issues)


def test_artifact_paths_hide_runtime_and_reject_escape(tmp_path):
    """The checker only accepts visible SQL artifacts within the session workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "create.sql").write_text(DDL)
    hidden = workspace / ".runtime"
    hidden.mkdir()
    (hidden / "config.sql").write_text(DDL)
    (tmp_path / "outside.sql").write_text(DDL)
    pipeline = SQLCheckerPipeline(workspace, None, RULES)
    assert pipeline._sql_path("create.sql") == workspace / "create.sql"
    for name in (".runtime/config.sql", "../outside.sql"):
        with pytest.raises(ValueError):
            pipeline._sql_path(name)


def test_outer_join_filter_is_preserved_without_repair(tmp_path):
    """Intentional post-join filtering must pass unchanged without consulting a model."""
    dml = """INSERT OVERWRITE biads.ads_result PARTITION (pt_d='$date')
SELECT f.user_id, COUNT(*) AS cnt
FROM src f LEFT JOIN dim d ON f.user_id = d.user_id
WHERE d.user_id IS NOT NULL GROUP BY f.user_id"""
    (tmp_path / "create.sql").write_text(DDL)
    (tmp_path / "insert.sql").write_text(dml)
    pipeline = SQLCheckerPipeline(tmp_path, None, RULES, {"dataops_validation_enabled": False})
    report = asyncio.run(pipeline.run("create.sql", "insert.sql", repair=False))
    assert report.get("status") == "PASS"
    assert report.get("issues") == []
    assert report.get("model_calls") == 0
    assert not report.get("changed_files")
    assert (tmp_path / "create.sql").read_text() == DDL
    assert (tmp_path / "insert.sql").read_text() == dml


def _patch(issue, file, old_text, new_text):
    return {"issue_id": issue.issue_id, "edits": [{"file": file, "old_text": old_text, "new_text": new_text}]}


def test_model_can_fix_issue_without_precomputed_replacement():
    """A model chooses where to move a DDL header even when no preset repair exists."""
    artifacts = {"ddl": "-- header\n" + DDL, "dml": DML}
    detector = RuleDetector(RULES)
    issue = next(item for item in detector.detect(artifacts) if item.rule_id == "DDL_START")
    assert issue.replacement is None
    response = _patch(issue, "ddl", artifacts.get("ddl"), DDL + ";\n-- header\n")
    updated = detector.apply(artifacts, issue, response)
    assert detector.detect(updated) == []
    assert updated.get("dml") == DML


def test_model_replacement_need_not_equal_detector_hint():
    """Equivalent SQL chosen by the model is allowed instead of an exact hint string."""
    artifacts = {"ddl": DDL, "dml": DML.replace("INSERT OVERWRITE", "INSERT INTO")}
    detector = RuleDetector(RULES)
    issue = next(item for item in detector.detect(artifacts) if item.rule_id == "WRITE_MODE")
    response = _patch(issue, "dml", "INSERT INTO", "INSERT OVERWRITE TABLE")
    updated = detector.apply(artifacts, issue, response)
    assert detector.detect(updated) == []


def test_model_patch_cannot_retarget_output():
    """Even otherwise valid SQL must not redirect either output table."""
    artifacts = {"ddl": "-- header\n" + DDL, "dml": DML}
    detector = RuleDetector(RULES)
    issue = next(item for item in detector.detect(artifacts) if item.rule_id == "DDL_START")
    response = _patch(issue, "ddl", artifacts.get("ddl"), DDL.replace("ads_result", "ads_other"))
    with pytest.raises(ValueError, match="target table"):
        detector.apply(artifacts, issue, response)


def test_patch_anchors_must_be_unique_and_non_overlapping():
    """Reject ambiguous anchors and intersecting edits against the original SQL."""
    artifacts = {"ddl": "-- header\n" + DDL, "dml": DML}
    detector = RuleDetector(RULES)
    issue = next(item for item in detector.detect(artifacts) if item.rule_id == "DDL_START")
    with pytest.raises(ValueError, match="unique"):
        detector.apply(artifacts, issue, _patch(issue, "dml", "user_id", "another_id"))
    response = _patch(issue, "ddl", artifacts.get("ddl"), DDL)
    response.get("edits").append({"file": "ddl", "old_text": "-- header\n", "new_text": ""})
    with pytest.raises(ValueError, match="overlap"):
        detector.apply(artifacts, issue, response)


def test_comment_after_semicolon_is_not_a_second_statement():
    """A comment-only SQLGlot Semicolon node must not invalidate a valid pair."""
    artifacts = {"ddl": DDL + ";\n-- header", "dml": DML + ";\n/* comment */"}
    assert RuleDetector(RULES).detect(artifacts) == []


def test_verification_rejects_reworded_issue_and_more_instances():
    """Changing the diagnosed text or adding another instance cannot bypass rule verification."""
    detector = RuleDetector(RULES)
    artifacts = {"ddl": "-- header\n" + DDL, "dml": DML.replace("INSERT OVERWRITE", "INSERT INTO")}
    before = detector.detect(artifacts)
    header = next(item for item in before if item.rule_id == "DDL_START")
    write = next(item for item in before if item.rule_id == "WRITE_MODE")
    with pytest.raises(ValueError, match="as many violations"):
        detector.verify_repair(before, [replace(header, fragment="-- different\n" + DDL), write], header)
    with pytest.raises(ValueError, match="increased"):
        detector.verify_repair(before, [write, replace(write, fragment="INSERT INTO other")], header)
    detector.verify_repair(before, [write], header)


def _dataops_pipeline(workspace, enabled=True):
    for kind, sql in (("create", DDL), ("insert", DML)):
        path = workspace / f"{kind}.sql"
        if not path.exists():
            path.write_text(sql)
    runtime = Runtime(Env(llm_configs={}, tavily_configs={}, modules={}, hooks={}, workspace_dir=workspace))
    return SQLCheckerPipeline(
        workspace,
        None,
        RULES,
        {"dataops_validation_enabled": enabled},
        tool_context=ToolExecutionContext(runtime=runtime),
    )


def test_rules_use_the_same_two_documents_as_generation():
    """Load current canonical files with exact source lines, without a checker-only copy."""
    detector = RuleDetector(RULES)
    assert set(detector.sources) == {"ddl_rules.md", "dml_rules.md"}
    assert not (RULES / "sql_checker_rules.md").exists()
    issue = next(item for item in detector.detect({"ddl": DDL, "dml": DML.replace("OVERWRITE", "INTO")}))
    assert "dml_rules.md:" in issue.evidence


def test_disabled_dataops_does_not_require_a_service(tmp_path):
    """The configuration switch bypasses DataOps without claiming execution validation."""
    report = asyncio.run(_dataops_pipeline(tmp_path, False).run("create.sql", "insert.sql"))
    assert report.get("status") == "PASS"
    assert report.get("dataops", {}).get("status") == "DISABLED"
    assert not list((tmp_path / ".runtime/sql_checkers").glob("dataops_*.json"))


def test_explicit_contract_is_checked_without_an_app_type_field(tmp_path):
    """Removing the benchmark trigger must not ignore contracts for other tasks."""
    (tmp_path / "contract.json").write_text(json.dumps({"output_columns": ["user_id", "cnt", "required_extra"]}))
    report = asyncio.run(_dataops_pipeline(tmp_path, False).run("create.sql", "insert.sql"))
    assert report.get("status") == "BLOCKED"
    assert {item.get("code") for item in report.get("contract_issues", [])} == {
        "OUTPUT_COLUMN_COUNT",
        "SELECT_COLUMN_COUNT",
    }


@pytest.mark.parametrize("explicit_config", [True, False])
def test_enabled_dataops_without_a_resource_is_not_a_pass(tmp_path, monkeypatch, explicit_config):
    """Explicit and default-on configurations must execute DataOps and reject skipped validation."""
    monkeypatch.setenv("DATAOPS_ENV", "integration")
    pipeline = _dataops_pipeline(tmp_path)
    if not explicit_config:
        pipeline.config.pop("dataops_validation_enabled")
    report = asyncio.run(pipeline.run("create.sql", "insert.sql"))
    assert report.get("status") == "BLOCKED"
    dataops = report.get("dataops", {})
    assert dataops.get("reason") == "validation_skipped"
    assert [item.get("file") for item in dataops.get("results", [])] == ["ddl"]
    assert report.get("changed_files") == []


@pytest.mark.parametrize("failing_kind", [None, "ddl", "dml"])
def test_dataops_order_and_persistent_retry_budget(tmp_path, monkeypatch, failing_kind):
    """Test service orchestration only; no LLM is replaced or called in these contract cases."""
    from dataagent.actions.tools.local_tool import dataops_validate_tool

    calls = []

    async def service_result(sql, *, attempt, prior_attempts, _tool_context):
        """Return a backend fixture to isolate retry and sequencing code from network execution."""
        kind = "ddl" if sql.startswith("CREATE") else "dml"
        calls.append((kind, attempt, len(prior_attempts)))
        return {"passed": kind != failing_kind, "log_analysis_summary": "服务检测结果"}

    monkeypatch.setattr(dataops_validate_tool, "dataops_validate_sql_with_log_analysis", service_result)
    for attempt_index in range(4):
        report = asyncio.run(_dataops_pipeline(tmp_path).run("create.sql", "insert.sql"))
        if failing_kind:
            # An Agent editing the SQL must not silently get a fresh retry budget.
            (tmp_path / "insert.sql").write_text(DML + f" -- revision {attempt_index}")
    if failing_kind is None:
        assert calls == [("ddl", 1, 0), ("dml", 1, 0)]
        assert report.get("status") == "PASS"
        assert report.get("dataops", {}).get("reused") is True
    else:
        kinds = ("ddl",) if failing_kind == "ddl" else ("ddl", "dml")
        assert calls == [(kind, attempt, attempt - 1) for attempt in (1, 2, 3) for kind in kinds]
        assert report.get("status") == "BLOCKED"
        assert report.get("dataops", {}).get("reason") == "attempts_exhausted"
    assert (tmp_path / "create.sql").read_text() == DDL
    assert (tmp_path / "insert.sql").read_text() == (DML + " -- revision 3" if failing_kind else DML)


def test_dataops_exception_is_bounded_and_does_not_expose_provider_text(tmp_path, monkeypatch):
    """An unavailable backend must fail closed and retain a sanitized attempt record."""
    from dataagent.actions.tools.local_tool import dataops_validate_tool

    async def unavailable_service(*args, **kwargs):
        """Exercise transport failure handling without replacing a model."""
        raise ConnectionError("private-provider-error")

    monkeypatch.setattr(dataops_validate_tool, "dataops_validate_sql_with_log_analysis", unavailable_service)
    report = asyncio.run(_dataops_pipeline(tmp_path).run("create.sql", "insert.sql"))
    assert report.get("status") == "BLOCKED"
    assert report.get("dataops", {}).get("status") == "ERROR"
    assert "private-provider-error" not in json.dumps(report)
    assert not list((tmp_path / ".runtime/sql_checkers").glob("*.lock"))
