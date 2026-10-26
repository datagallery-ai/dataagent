"""Ensure fixed checks do not rewrite valid task-dependent SQL semantics."""

import asyncio
from pathlib import Path

import pytest

from dataagent.core.suite.builtin_suites.de_agent.tools.sql_checkers import SQLCheckerPipeline
from dataagent.core.suite.builtin_suites.de_agent.tools.sql_rule_detectors import RuleDetector
from dataagent.core.suite.builtin_suites.de_agent.tools.verify import _validate_field_types

RULES = Path(__file__).resolve().parents[3] / "dataagent/core/suite/builtin_suites/de_agent/skill/sql_rules/references"
DDL = """CREATE EXTERNAL TABLE IF NOT EXISTS biads.ads_result (
user_id STRING COMMENT '用户', metric {dtype} COMMENT '指标'
) COMMENT '结果表' PARTITIONED BY (pt_d STRING COMMENT '天分区') STORED AS ORC"""
PREFIX = "INSERT OVERWRITE biads.ads_result PARTITION (pt_d='$date') "


@pytest.mark.parametrize(
    ("dtype", "query"),
    [
        ("DECIMAL(38,6)", "SELECT user_id, numerator / denominator AS metric FROM src"),
        ("DECIMAL(38,6)", "SELECT user_id, numerator / -2.0 AS metric FROM src"),
        ("DECIMAL(26,2)", "SELECT user_id, CAST(numerator / denominator AS DECIMAL(26,2)) AS metric FROM src"),
        ("DECIMAL(26,3)", "SELECT user_id, CAST(numerator / denominator AS DECIMAL(26,3)) AS metric FROM src"),
        ("DECIMAL(38,6)", "SELECT user_id, CAST(numerator AS FLOAT) AS metric FROM src"),
        ("STRING", "SELECT user_id, CAST(numerator AS STRING) AS metric FROM src"),
        ("DECIMAL(38,6)", "SELECT user_id, CASE WHEN flag='game' THEN numerator / denominator END AS metric FROM src"),
        ("DECIMAL(38,6)", "SELECT user_id, SUM(ROUND(numerator, 2)) AS metric FROM src GROUP BY user_id"),
        (
            "DECIMAL(38,6)",
            "SELECT f.user_id, d.value AS metric FROM src f LEFT JOIN dim d ON f.user_id=d.user_id "
            "WHERE !bicoredata.IsEmpty(d.kind)",
        ),
        ("DECIMAL(38,6)", "SELECT f.user_id, d.value AS metric FROM src f LEFT JOIN dim d ON f.user_id=d.user_id"),
    ],
)
def test_uncovered_business_variants_are_not_rewritten(tmp_path, dtype, query):
    """A static PASS does not claim coverage of task-dependent ratio, NULL or rounding rules."""
    artifacts = {"ddl": DDL.format(dtype=dtype), "dml": PREFIX + query}
    for kind, content in artifacts.items():
        (tmp_path / f"{kind}.sql").write_text(content)
    pipeline = SQLCheckerPipeline(tmp_path, None, RULES, {"dataops_validation_enabled": False})
    report = asyncio.run(pipeline.run("ddl.sql", "dml.sql", repair=True))
    assert report.get("status") == "PASS", report
    assert report.get("model_calls") == 0
    assert not report.get("changed_files")
    for kind, content in artifacts.items():
        assert (tmp_path / f"{kind}.sql").read_text() == content


@pytest.mark.parametrize("dtype", ["DECIMAL(26,2)", "DECIMAL(26,3)"])
def test_legacy_gate_accepts_the_same_numeric_types(dtype):
    """Both static gates accept explicit decimal precisions from the canonical whitelist."""
    block = f"metric {dtype} COMMENT '指标'"
    assert _validate_field_types(block, block) == []


def test_fixed_error_survives_uncovered_business_semantics():
    """A valid ratio expression does not hide a genuinely invalid write mode."""
    dml = PREFIX.replace("INSERT OVERWRITE", "INSERT INTO") + "SELECT user_id, a/b AS metric FROM src"
    issues = RuleDetector(RULES).detect({"ddl": DDL.format(dtype="DECIMAL(38,6)"), "dml": dml})
    assert {issue.rule_id for issue in issues} == {"WRITE_MODE"}
