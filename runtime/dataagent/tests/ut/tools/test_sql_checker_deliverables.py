"""Regression coverage for the fixed checks lost when replacing the delivery gate."""

from pathlib import Path

import pytest

from dataagent.core.suite.builtin_suites.de_agent.tools.sql_rule_detectors import RuleDetector

RULES = Path(__file__).resolve().parents[3] / "dataagent/core/suite/builtin_suites/de_agent/skill/sql_rules/references"
DDL = """CREATE EXTERNAL TABLE IF NOT EXISTS biads.ads_result (
user_id STRING COMMENT '用户', cnt BIGINT COMMENT '计数'
) COMMENT '结果表' PARTITIONED BY (pt_d STRING COMMENT '天分区') STORED AS ORC"""
DML = """INSERT OVERWRITE biads.ads_result PARTITION (pt_d='$date')
SELECT user_id, COUNT(*) AS cnt FROM src GROUP BY user_id"""


@pytest.mark.parametrize(
    ("before", "after", "rule"),
    [
        ("CREATE", "-- header\nCREATE", "DDL_START"),
        ("biads.ads_result", "ads_result", "TARGET_DATABASE"),
        ("ads_result", "BadName", "DDL_NAMING"),
        ("user_id STRING", "User_ID STRING", "DDL_NAMING"),
        ("cnt BIGINT", "cnt BOOLEAN", "DDL_TYPE"),
        ("cnt BIGINT", "cnt DOUBLE", "DDL_TYPE"),
        ("cnt BIGINT", "cnt FLOAT", "DDL_TYPE"),
        ("cnt BIGINT", "cnt DECIMAL", "DDL_TYPE"),
        ("user_id STRING", "user_id TEXT", "DDL_TYPE"),
        ("user_id STRING", "user_id VARCHAR(20)", "DDL_TYPE"),
        (" COMMENT '用户'", "", "DDL_COMMENT"),
        (" COMMENT '结果表'", "", "DDL_TABLE_COMMENT"),
        ("'用户'", "'x用户'", "DDL_COMMENT_CHARS"),
        ("'结果表'", "'结果@表'", "DDL_COMMENT_CHARS"),
        ("pt_d STRING", "pt_d BIGINT", "DDL_PARTITION"),
        ("pt_d STRING", "day STRING", "DDL_PARTITION"),
        ("'天分区'", "'日期'", "DDL_PARTITION"),
        ("user_id STRING", "pt_d STRING", "DDL_PARTITION"),
        ("user_id STRING", "user_id STRING PRIMARY KEY", "DDL_PRIMARY_KEY"),
    ],
)
def test_old_ddl_contract_is_enforced(before, after, rule):
    """Reject missing declaration requirements with an original-check provenance."""
    issues = RuleDetector(RULES).detect({"ddl": DDL.replace(before, after), "dml": DML})
    issue = next(item for item in issues if item.rule_id == rule)
    assert ("ddl_rules.md:" if rule == "DDL_TYPE" else "verify.py::") in issue.evidence
    assert "severity" not in issue.to_dict()


@pytest.mark.parametrize(
    ("expression", "rule", "replacement"),
    [
        ("TRY_CAST(cnt AS BIGINT)", "UNSUPPORTED_FUNCTION", None),
        ("ARRAY_AGG(cnt)", "UNSUPPORTED_FUNCTION", None),
        ("UNIX_TO_STR(cnt)", "UNIX_TO_STR", "from_unixtime"),
        ("REGEXP_SPLIT(user_id, ',')", "REGEXP_SPLIT", "SPLIT"),
        ("ARRAY_UNIQUE_AGG(cnt)", "ARRAY_UNIQUE_AGG", "COLLECT_SET"),
        ("CAST(COALESCE(cnt, 0) AS TEXT)", "CAST_TEXT", "STRING"),
        ("DateFormat(user_id)", "UDF_PREFIX", "bicoredata.DateFormat"),
        ("ConcatWithRank(user_id, cnt, ',')", "UDF_PREFIX", "bicoredata.ConcatWithRank"),
        ("other.DateFormat(user_id)", "UDF_PREFIX", None),
        ("CONCAT_WS(',', COLLECT_LIST(user_id ORDER BY cnt))", "COLLECT_LIST_ORDER", None),
    ],
)
def test_old_dml_function_checks_and_local_repairs(expression, rule, replacement):
    """Detect raw function spellings before normalization can erase incompatibility."""
    detector = RuleDetector(RULES)
    artifacts = {"ddl": DDL, "dml": DML.replace("COUNT(*)", expression)}
    issue = next(item for item in detector.detect(artifacts) if item.rule_id == rule)
    assert issue.replacement == replacement
    if replacement is not None:
        response = {
            "issue_id": issue.issue_id,
            "edits": [{"file": issue.file, "old_text": issue.fragment, "new_text": replacement}],
        }
        updated = detector.apply(artifacts, issue, response)
        assert not any(item.rule_id == rule for item in detector.detect(updated))


@pytest.mark.parametrize(
    ("dml", "rule"),
    [
        (DML.replace("biads.ads_result", "ads_result"), "TARGET_DATABASE"),
        (DML.replace("pt_d='$date'", "pt_d"), "STATIC_PARTITION"),
        (DML.replace("$date", "${start_time,\n-7,yyyyMMdd}"), "SCHEDULER_SINGLE_LINE"),
        (DML.replace("FROM src", "FROM src JOIN dim ON src.user_id=dim.user_id"), "JOIN_TYPE"),
        (DML + " QUALIFY ROW_NUMBER() OVER (ORDER BY user_id)=1", "QUALIFY"),
        (
            DML.replace("FROM src", "FROM src LATERAL VIEW explode(ids) e AS id LEFT JOIN dim ON id=dim.user_id"),
            "LATERAL_VIEW_JOIN",
        ),
    ],
)
def test_old_dml_structure_checks(dml, rule):
    """Keep structural failures visible even when Spark-dialect parsing accepts them."""
    assert any(item.rule_id == rule for item in RuleDetector(RULES).detect({"ddl": DDL, "dml": dml}))


def test_comments_and_strings_cannot_trigger_dialect_rules():
    """Only source code is checked for forbidden functions/types, not examples in values."""
    ddl = DDL.replace("'用户'", "'DOUBLE DATE PRIMARY KEY' ")
    dml = DML.replace("COUNT(*)", "'TRY_CAST(1) QUALIFY CAST(1 AS TEXT) DateFormat()'")
    dml += " -- UNIX_TO_STR(x) LATERAL VIEW ... JOIN ..."
    assert RuleDetector(RULES).detect({"ddl": ddl, "dml": dml}) == []


def test_lateral_subquery_then_join_is_allowed():
    """Closing the lateral subquery permits a JOIN in its outer scope."""
    dml = DML.replace(
        "FROM src",
        "FROM (SELECT user_id FROM src LATERAL VIEW explode(ids) e AS id) t LEFT JOIN dim ON t.user_id=dim.user_id",
    )
    assert not any(item.rule_id == "LATERAL_VIEW_JOIN" for item in RuleDetector(RULES).detect({"ddl": DDL, "dml": dml}))


def test_cte_scope_preserves_nested_alias_and_rejects_missing_column():
    """Avoid the legacy CTE scanner's false missing-rn error across nested scopes."""
    prefix = "WITH base AS (SELECT user_id, COUNT(*) AS cnt FROM src GROUP BY user_id) "
    dml = prefix + DML.replace(
        "FROM src GROUP BY user_id",
        "FROM (SELECT user_id, cnt, ROW_NUMBER() OVER (ORDER BY user_id) AS rn FROM base) t "
        "WHERE rn=1 GROUP BY user_id",
    )
    detector = RuleDetector(RULES)
    assert detector.detect({"ddl": DDL, "dml": dml}) == []
    broken = prefix + DML.replace("COUNT(*)", "SUM(base.missing)").replace("FROM src", "FROM base")
    assert any(item.rule_id == "COLUMN_SCOPE" for item in detector.detect({"ddl": DDL, "dml": broken}))
