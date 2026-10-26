"""Check explicit delivery evidence without benchmark-specific business assumptions."""

import json

import pytest

from dataagent.core.suite.builtin_suites.de_agent.tools.delivery_contract import (
    check_contract,
    check_static_semantics,
)

DDL = """CREATE EXTERNAL TABLE IF NOT EXISTS biads.ads_result (
  device_id STRING,
  app_type STRING,
  seq STRING
) PARTITIONED BY (pt_d STRING) STORED AS ORC"""


@pytest.mark.parametrize("table", ["ads_noah_app_qa_result_analysis_dm", "app_info", "exposure"])
def test_names_do_not_imply_join_policy(tmp_path, table):
    """A physical table name alone does not prove its role or the intended retained population."""
    dml = f"SELECT f.device_id FROM fact f INNER JOIN biads.{table} d ON f.id=d.id"
    assert check_static_semantics(DDL, dml, tmp_path) == []


def test_app_type_does_not_enable_hidden_business_rules(tmp_path):
    """The app_type name does not enable a special filter, dedup or JOIN-direction policy."""
    dml = (
        "WITH exposure AS (SELECT imei_orgi FROM biads.ads_noah_common_exposure_imei_dm), "
        "usage AS (SELECT imei, app_type FROM biads.ads_noah_common_user_use_detail_dm), "
        "joined AS (SELECT e.imei_orgi FROM exposure e LEFT JOIN usage u ON e.imei_orgi=u.imei) "
        "SELECT device_id, app_type, CONCAT(app_type, ':', 'x') AS seq "
        "FROM joined GROUP BY device_id, app_type"
    )
    assert check_static_semantics(DDL, dml, tmp_path) == []


def test_nullable_side_filter_is_not_unconditionally_rejected(tmp_path):
    """Canonical category filtering is allowed without a blanket LEFT JOIN predicate ban."""
    dml = (
        "SELECT f.device_id, d.app_type, CONCAT(d.app_type, ':', 'x') AS seq "
        "FROM fact f LEFT JOIN dim d ON f.id=d.id "
        "WHERE NOT bicoredata.IsEmpty(d.app_type) GROUP BY f.device_id,d.app_type"
    )
    assert check_static_semantics(DDL, dml, tmp_path) == []


@pytest.mark.parametrize(("expected", "actual", "blocked"), [("LEFT", "INNER", True), ("FULL", "FULL OUTER", False)])
def test_explicit_metadata_join_evidence_is_preserved(tmp_path, expected, actual, blocked):
    """Only an explicit relation requirement, including OUTER syntax, constrains the join."""
    relations = [{"src": "biads.fact.id", "target": "biads.dim.id", "rel_type": expected}]
    (tmp_path / "schema_schemair.md").write_text("schema_ir = {}\njoin_relations = " + json.dumps(relations))
    dml = f"SELECT f.device_id FROM biads.fact f {actual} JOIN biads.dim d ON f.id=d.id"
    findings = check_static_semantics(DDL, dml, tmp_path)
    assert bool(findings) == blocked
    if blocked:
        assert findings[0].get("code") == "SQL_JOIN_TYPE_MISMATCH"


def test_explicit_contract_constraints_are_preserved():
    """Keep output, filter and JOIN constraints declared by the actual task contract."""
    contract = {
        "output_columns": ["device_id", "app_type", "seq"],
        "required_filters": [{"field": "app_type", "operator": "not_empty"}],
        "joins": [{"right": "dim", "type": "LEFT"}],
    }
    dml = "SELECT device_id, app_type, seq FROM fact INNER JOIN dim ON fact.id=dim.id"
    findings = check_contract(DDL, dml, contract)
    assert {item.get("code") for item in findings} == {"REQUIRED_FILTER_MISSING", "CONTRACT_JOIN_MISMATCH"}
    correct = dml.replace("INNER JOIN", "LEFT JOIN") + " WHERE NOT bicoredata.IsEmpty(app_type)"
    assert check_contract(DDL, correct, contract) == []
