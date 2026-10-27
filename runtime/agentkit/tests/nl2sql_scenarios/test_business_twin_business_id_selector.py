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
"""Business-twin business-ID selection: catalog contract and column-only routing.

Ported from the legacy ``tests/ut/agents/nl2sql/test_business_twin_business_id_selector.py``
(br_release_930). The selector's dead ``question`` parameter was removed in the
2026-10-23 cleanup, so every case is now driven by the column payload alone; the
parametrize ids keep the legacy question each case was ported from.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scenarios.udn.business_twin.data import business_twin_business_id_catalog as catalog_module
from scenarios.udn.business_twin.data import business_twin_business_id_selector as selector

CATALOG_PATH = Path(catalog_module.__file__).resolve().parent / "business_twin_business_id_catalog.json"


def test_business_id_catalog_is_a_packaged_json_resource() -> None:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

    assert payload["version"] == 1
    assert len(payload["business_schemas"]) == 21
    assert payload["business_schemas"]["dw1745159007"]["dimensions"] == [
        "city",
        "county",
        "tai",
        "gnb",
        "cell_id",
        "guarantee_group",
        "app_id",
        "sub_app_id",
        "custom_group",
        "15min_granularity",
        "1h_granularity",
        "1d_granularity",
    ]
    # The high-rail routing rules were deleted as dead code in the 2026-10-23 cleanup.
    assert "high_rail" not in payload["business_rules"]


def test_selects_general_experience_from_bare_column_list() -> None:
    assert (
        selector.select_business_twin_business_id(
            ["downlink_traffic", "downlink_duration", "county", "guarantee_group"]
        ).business_id
        == "dw1745159007"
    )


def test_known_extra_metric_is_classified_without_dimension_error() -> None:
    assert (
        selector.select_business_twin_business_id(
            ["downlink_traffic", "downlink_duration", "county", "assurance_users"]
        ).business_id
        == "dw1745159007"
    )


def test_extraction_keeps_required_dimensions_and_granularity() -> None:
    result = selector.select_business_twin_business_id(
        ["downlink_traffic", "downlink_duration", "guarantee_group", "1h_granularity", "tai"],
    )

    assert result.business_id == "dw1745159007"
    assert result.metrics == frozenset({"downlink_traffic", "downlink_duration"})
    assert result.dimensions == frozenset({"guarantee_group", "tai"})
    assert result.granularity == "1h"


def test_extraction_reports_first_granularity_and_none_without_one() -> None:
    assert (
        selector.select_business_twin_business_id(
            ["downlink_traffic", "1d_granularity", "1h_granularity"]
        ).granularity
        == "1d"
    )
    assert selector.select_business_twin_business_id(["downlink_traffic"]).granularity is None


def test_extraction_normalizes_case_deviated_columns() -> None:
    result = selector.select_business_twin_business_id(
        ["Downlink_Traffic", "DOWNLINK_DURATION", "GUARANTEE_GROUP", "1H_Granularity", "Tai"],
    )

    assert result.business_id == "dw1745159007"
    assert result.metrics == frozenset({"downlink_traffic", "downlink_duration"})
    assert result.dimensions == frozenset({"guarantee_group", "tai"})
    assert result.granularity == "1h"


@pytest.mark.parametrize(
    ("columns", "expected"),
    [
        pytest.param(["recv_ue_logo_online_users_of_amf"], "dw1745159005", id="查询AMF网元在线用户数"),
        pytest.param(
            ["to_amf_policy_auth_request_success_times_of_pcf"],
            "dw1745159006",
            id="查询PCF实例策略授权成功次数",
        ),
        pytest.param(
            ["assurance_abnormal_release_times_of_nwdaf"],
            "dw1745159003",
            id="查询NWDAF实例保障异常释放次数",
        ),
        pytest.param(
            ["de_acc_num_of_rail_usr_of_nwdaf", "ne_name"],
            "dw1745159003",
            id="查询高铁用户接入高铁专网次数",
        ),
        pytest.param(["crh_users"], "dw1745159030", id="查询高铁用户数"),
        pytest.param(["crh_users", "crh_group"], "dw1745159030", id="查询高铁分群用户数"),
        pytest.param(["crh_ride_times", "gpsi"], "dw1745159012", id="查询高铁乘坐次数"),
        pytest.param(
            ["crh_trigger_first_n23_create_times", "gnb"],
            "dw1745159031",
            id="查询高铁首次N23会话创建上报次数",
        ),
        pytest.param(
            ["downlink_traffic", "crh_section_id"],
            "dw1745159032",
            id="查询高铁线路段下行流量",
        ),
        pytest.param(
            ["service_times", "crh_railway_id"],
            "dw1745159033",
            id="查询高铁线路业务使用次数",
        ),
        pytest.param(
            ["assurance_users", "crh_railway_id"],
            "dw1745159034",
            id="查询高铁线路保障用户数",
        ),
        pytest.param(["downlink_traffic"], "dw1745159007", id="查询高铁下行流量"),
        pytest.param(["cell_prb_ul_usage"], "dw1745159004", id="查询上行PRB使用量"),
        pytest.param(["assurance_times"], "dw1745159008", id="查询保障次数"),
        pytest.param(["assurance_users", "ne_name"], "dw1745159028", id="查询网元维度保障用户数"),
        pytest.param(["assurance_users", "cell_id"], "dw1745159037", id="查询小区维度保障用户数"),
        pytest.param(["assurance_times", "term_brand"], "dw1745159016", id="查询各终端品牌保障次数"),
        pytest.param(
            ["assurance_times", "default5qi_group"],
            "dw1745159017",
            id="查询各5QI分群保障次数",
        ),
        pytest.param(
            ["avg_qoe", "mos_times", "guarantee_group"],
            "dw1745159007",
            id="查询保障与非保障用户MOS",
        ),
        pytest.param(
            ["avg_qoe", "mos_times", "guarantee_group", "term_brand"],
            "dw1745159014",
            id="查询各终端品牌保障与非保障用户MOS",
        ),
        pytest.param(
            ["avg_qoe", "mos_times", "guarantee_group", "default5qi_group"],
            "dw1745159015",
            id="查询各5QI分群保障与非保障用户MOS",
        ),
        pytest.param([], "dw1745159007", id="普通查询"),
    ],
)
def test_column_only_routes(columns: list[str], expected: str) -> None:
    assert selector.select_business_twin_business_id(columns).business_id == expected


@pytest.mark.parametrize(
    ("columns", "expected"),
    [
        (["mos_sec3_times"], "dw1745159007"),
        (["mos_sec1_times", "term_brand"], "dw1745159014"),
        (["mos_sec2_times", "default5qi_group"], "dw1745159015"),
        (["most_resolution1080p_times", "ne_name"], "dw1745159027"),
    ],
)
def test_normalizes_physical_distribution_metrics(columns: list[str], expected: str) -> None:
    assert selector.select_business_twin_business_id(columns).business_id == expected


@pytest.mark.parametrize(
    ("columns", "expected"),
    [
        (["MOS_SEC3_TIMES"], "dw1745159007"),
        (["Mos_Sec1_Times", "TERM_BRAND"], "dw1745159014"),
        (["MOST_RESOLUTION1080P_TIMES", "NE_NAME"], "dw1745159027"),
        (["ASSURANCE_TIMES", "Term_Brand"], "dw1745159016"),
    ],
)
def test_routes_case_deviated_columns(columns: list[str], expected: str) -> None:
    assert selector.select_business_twin_business_id(columns).business_id == expected


def test_ignores_case_deviated_temporal_columns_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(selector.logger, "warning", warnings.append)

    result = selector.select_business_twin_business_id(["Downlink_Traffic", "TIME"])

    assert result.business_id == "dw1745159007"
    assert result.dimensions == frozenset()
    assert warnings == []


def test_ignores_duplicate_temporal_and_unknown_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(selector.logger, "warning", warnings.append)

    result = selector.select_business_twin_business_id(
        ["downlink_traffic", "downlink_traffic", "time", "invented_column", "city"]
    )

    assert result.business_id == "dw1745159007"
    assert warnings == ["Ignoring unknown business-twin extraction columns: invented_column"]


def test_all_ignored_or_unknown_columns_use_general_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(selector.logger, "warning", lambda _message: None)

    assert (
        selector.select_business_twin_business_id(
            ["time", "invented_column"],
        ).business_id
        == "dw1745159007"
    )


@pytest.mark.parametrize("payload", [None, {}, "downlink_traffic", ["city", 1]])
def test_rejects_non_string_array_payload(payload: object) -> None:
    with pytest.raises(ValueError, match="字符串数组"):
        selector.select_business_twin_business_id(payload)


def test_value_sensitive_mobile_game_preference_is_removed() -> None:
    assert (
        selector.select_business_twin_business_id(
            ["delay_e2e", "delay_e2e_times", "app_id", "city", "term_brand"]
        ).business_id
        == "dw1745159014"
    )
