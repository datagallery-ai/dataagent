from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_STATUS = (
    Path(__file__).resolve().parents[3]
    / "dataagent"
    / "core"
    / "suite"
    / "builtin_suites"
    / "data_analysis"
    / "tools"
    / "workflow"
    / "status.py"
)
_SPEC = importlib.util.spec_from_file_location("data_analysis_workflow_status", _STATUS)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
validate_receipt = _MODULE.validate_receipt


@pytest.mark.parametrize(
    ("kind", "uri"),
    [
        ("warehouse_table", "hive://analytics/scored_users"),
        ("clickhouse_table", "clickhouse://analytics/scored_users"),
    ],
)
def test_validate_receipt_accepts_generic_and_legacy_warehouse_tables(
    kind: str,
    uri: str,
) -> None:
    receipt = validate_receipt(
        {
            "summary": "done",
            "artifacts": [{"kind": kind, "uri": uri}],
        }
    )

    assert receipt["artifacts"] == [
        {
            "kind": kind,
            "uri": uri,
            "name": "analytics.scored_users",
        }
    ]


@pytest.mark.parametrize(
    "uri",
    ["analytics/scored_users", "hive://analytics", "hive://1bad/table", "bad scheme://db/table"],
)
def test_validate_receipt_rejects_invalid_warehouse_uri(uri: str) -> None:
    with pytest.raises(ValueError):
        validate_receipt(
            {
                "summary": "bad",
                "artifacts": [{"kind": "warehouse_table", "uri": uri}],
            }
        )
