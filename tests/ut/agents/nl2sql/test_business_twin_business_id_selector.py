from __future__ import annotations

import json

import pytest

from dataagent.agents.nl2sql.utils import business_twin_business_id_selector as selector
from dataagent.utils.runtime_paths import dataagent_package_path


def test_business_id_catalog_is_a_packaged_json_resource() -> None:
    catalog_path = dataagent_package_path(
        "agents",
        "nl2sql",
        "utils",
        "business_twin_business_id_catalog.json",
    )

    payload = json.loads(catalog_path.read_text(encoding="utf-8"))

    assert payload["version"] == 1
    assert len(payload["business_schemas"]) == 19
    assert payload["business_schemas"]["dw1745159007"]["dimensions"] == [
        "app_id",
        "cell_id",
        "city",
        "county",
        "custom_group",
        "gnb",
        "guarantee_group",
        "sub_app_id",
        "tai",
    ]


def test_selects_general_experience_from_bare_column_list() -> None:
    assert (
        selector.select_business_twin_business_id(
            "5月宜兴市保障用户网络下行速率提升百分比前20个",
            ["downlink_traffic", "downlink_duration", "county", "guarantee_group"],
        )
        == "dw1745159007"
    )


def test_known_extra_metric_is_classified_without_dimension_error() -> None:
    assert (
        selector.select_business_twin_business_id(
            "5月宜兴市保障用户网络下行速率提升百分比前20个",
            ["downlink_traffic", "downlink_duration", "county", "assurance_users"],
        )
        == "dw1745159007"
    )


@pytest.mark.parametrize(
    ("question", "columns", "expected"),
    [
        ("查询AMF网元在线用户数", [], "dw1745159005"),
        ("查询PCF实例策略授权成功次数", [], "dw1745159006"),
        ("查询NWDAF实例保障异常释放次数", [], "dw1745159003"),
        ("查询高铁用户数", ["crh_users"], "dw1745159021"),
        ("查询高铁乘坐次数", ["crh_ride_times", "gpsi"], "dw1745159012"),
        ("查询高铁分群用户数", ["crh_users", "crh_group"], "dw1745159010"),
        ("查询高铁下行流量", ["downlink_traffic"], "dw1745159011"),
        ("查询上行PRB使用量", ["cell_prb_ul_usage"], "dw1745159004"),
        ("查询保障次数", ["assurance_times"], "dw1745159008"),
        (
            "查询各终端品牌保障次数",
            ["assurance_times", "term_brand"],
            "dw1745159016",
        ),
        (
            "查询各5QI分群保障次数",
            ["assurance_times", "default5qi_group"],
            "dw1745159017",
        ),
        (
            "查询保障与非保障用户MOS",
            ["avg_qoe", "mos_times", "mos4_qds"],
            "dw1745159020",
        ),
        (
            "查询各终端品牌保障与非保障用户MOS",
            ["avg_qoe", "mos_times", "mos4_qds", "term_brand"],
            "dw1745159018",
        ),
        (
            "查询各5QI分群保障与非保障用户MOS",
            ["avg_qoe", "mos_times", "mos4_qds", "default5qi_group"],
            "dw1745159019",
        ),
        ("普通查询", [], "dw1745159007"),
    ],
)
def test_column_only_routes(question: str, columns: list[str], expected: str) -> None:
    assert selector.select_business_twin_business_id(question, columns) == expected


@pytest.mark.parametrize(
    ("columns", "expected"),
    [
        (["mos_sec3_users"], "dw1745159009"),
        (["mos_sec1_users", "term_brand"], "dw1745159018"),
        (["mos_sec2_users", "default5qi_group"], "dw1745159019"),
    ],
)
def test_normalizes_physical_distribution_metrics(columns: list[str], expected: str) -> None:
    assert selector.select_business_twin_business_id("查询MOS分段用户数", columns) == expected


def test_ignores_duplicate_temporal_and_unknown_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(selector.logger, "warning", warnings.append)

    result = selector.select_business_twin_business_id(
        "查询无锡下行流量",
        ["downlink_traffic", "downlink_traffic", "time", "invented_column", "city"],
    )

    assert result == "dw1745159007"
    assert warnings == ["Ignoring unknown business-twin extraction columns: invented_column"]


def test_all_ignored_or_unknown_columns_use_general_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(selector.logger, "warning", lambda _message: None)

    assert (
        selector.select_business_twin_business_id(
            "普通查询",
            ["time", "invented_column"],
        )
        == "dw1745159007"
    )


@pytest.mark.parametrize("payload", [None, {}, "downlink_traffic", ["city", 1]])
def test_rejects_non_string_array_payload(payload: object) -> None:
    with pytest.raises(ValueError, match="字符串数组"):
        selector.select_business_twin_business_id("查询下行流量", payload)


def test_value_sensitive_mobile_game_preference_is_removed() -> None:
    assert (
        selector.select_business_twin_business_id(
            "查询无锡市华为终端手游平均端到端时延",
            ["delay_e2e", "delay_e2e_times", "app_id", "city", "term_brand"],
        )
        == "dw1745159014"
    )
