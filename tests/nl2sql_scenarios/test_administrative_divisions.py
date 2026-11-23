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
"""Administrative-division matching: packaged index contract, alias matching, rule injection.

Ported from the legacy ``tests/ut/agents/nl2sql/test_administrative_divisions.py``
(br_release_930); the perceptor integration now runs through the scenario pack's
``perceptor.schema_context`` hook instead of the legacy perceptor node.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from nl2sql.hooks import PERCEPTOR_SCHEMA_CONTEXT
from nl2sql.nodes.perceptor import PerceptorNode
from scenarios.udn.business_twin import table_selector as table_selector_module
from scenarios.udn.business_twin.data import (
    administrative_divisions as administrative_divisions_module,
)
from scenarios.udn.business_twin.data.administrative_divisions import (
    _load_administrative_division_index,
    format_administrative_division_rules,
    match_administrative_divisions,
)
from scenarios.udn.business_twin.table_selector import BusinessTwinTableSelector

from nl2sql_scenarios.support import make_context, scripted_model

PACK_DIR = Path(table_selector_module.__file__).resolve().parent
DATA_DIR = PACK_DIR / "data"
INDEX_PATH = DATA_DIR / "china_administrative_division_aliases.json"


def test_administrative_division_index_stays_with_scenario_data() -> None:
    # The alias index is a packaged scenario asset: it must stay beside its loader
    # in the pack's data directory, and the pack's declarative assets must not
    # declare it (the loader resolves the path module-relatively, never via config).
    assert administrative_divisions_module._INDEX_PATH == INDEX_PATH
    assert INDEX_PATH.is_file()
    assert "administrative_divisions" not in (PACK_DIR / "dimensions.yaml").read_text(
        encoding="utf-8"
    )


def test_administrative_division_alias_index_contract() -> None:
    payload = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    divisions = payload["divisions"]
    identities = {(item["level"], item["code"]) for item in divisions}

    assert payload["version"] == 1
    assert payload["source"]["commit"] == "c6c6e35bea3066d674efe2cded189dc57a86e7d8"
    assert len(identities) == len(divisions)
    assert sum(item["level"] == "province" for item in divisions) == 34
    assert sum(item["level"] == "county" for item in divisions) == 3233
    assert all(len(item["code"]) == 6 and item["code"].isdigit() for item in divisions)

    ninghe = next(
        item for item in divisions if (item["level"], item["code"]) == ("county", "120117")
    )
    assert {"宁河区", "宁河", "ning he", "ninghe", "ninghe district"} <= set(ninghe["aliases"])


@pytest.mark.parametrize("question", ["查询广东省深圳市南山区的流量", "查询广东深圳南山的流量"])
def test_matches_multiple_chinese_levels(question: str) -> None:
    matches = match_administrative_divisions(question)

    assert [(item["level"], item["code"]) for item in matches] == [
        ("province", "440000"),
        ("city", "440300"),
        ("county", "440305"),
    ]


@pytest.mark.parametrize("question", ["shi jia zhuang", "shijiazhuang", "Shijiazhuang City"])
def test_matches_spaced_compact_and_suffixed_pinyin(question: str) -> None:
    matches = match_administrative_divisions(question)

    assert [(item["level"], item["code"]) for item in matches] == [("city", "130100")]


def test_matches_normalized_english_and_prefers_specific_level() -> None:
    matches = match_administrative_divisions("Compare HONG_KONG with Ninghe-District")

    assert [(item["level"], item["code"]) for item in matches] == [
        ("city", "810000"),
        ("county", "120117"),
    ]


def test_parent_context_disambiguates_same_county_name() -> None:
    matches = match_administrative_divisions("北京市朝阳区")

    assert [item["code"] for item in matches if item["level"] == "county"] == ["110105"]


def test_unresolved_same_level_name_keeps_all_candidates() -> None:
    matches = match_administrative_divisions("查询朝阳区")

    assert {item["code"] for item in matches} == {"110105", "220104"}


def test_formats_rules_and_returns_empty_for_no_match() -> None:
    assert format_administrative_division_rules("普通业务查询") == ""
    assert format_administrative_division_rules("深圳市南山区") == (
        "\n\n## 行政区划匹配\n"
        "- 深圳市：city，行政编码为 440300（广东省）\n"
        "- 南山区：county，行政编码为 440305（广东省 / 深圳市）"
    )


def test_deduplicates_repeated_location_in_first_occurrence_order() -> None:
    matches = match_administrative_divisions("深圳市和深圳")

    assert [(item["level"], item["code"]) for item in matches] == [("city", "440300")]


def test_fixed_index_is_cached_once() -> None:
    _load_administrative_division_index.cache_clear()
    try:
        match_administrative_divisions("深圳")
        match_administrative_divisions("无锡")

        assert _load_administrative_division_index.cache_info().currsize == 1
    finally:
        _load_administrative_division_index.cache_clear()


def test_missing_index_fails_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing_path = tmp_path / "missing-admin-index.json"
    monkeypatch.setattr(administrative_divisions_module, "_INDEX_PATH", missing_path)
    _load_administrative_division_index.cache_clear()
    try:
        assert match_administrative_divisions("深圳") == []
        assert format_administrative_division_rules("深圳") == ""
    finally:
        _load_administrative_division_index.cache_clear()


async def test_business_twin_aprocess_appends_administrative_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_context("udn.business_twin", scripted_model(), None)
    selector = BusinessTwinTableSelector(ctx, base=PACK_DIR, semantic_client=None)
    monkeypatch.setattr(selector, "_sql_rules", "existing rule")
    monkeypatch.setattr(table_selector_module, "schema_to_ddl", lambda *_args: "DDL")

    async def fake_schema_linking(_question: str):
        return {}, [], {}, False

    monkeypatch.setattr(selector, "_schema_linking", fake_schema_linking)

    node = PerceptorNode(config=ctx.config)
    node.bind_hooks({PERCEPTOR_SCHEMA_CONTEXT: selector.select})
    state = {"question": "查询深圳市南山区流量"}

    result = await node._aprocess(state)

    assert result["sql_rules"].startswith(f"existing rule\n现在为{date.today().year}年")
    assert "\n\n## 行政区划匹配" in result["sql_rules"]
    assert "- 深圳市：city，行政编码为 440300（广东省）" in result["sql_rules"]
    assert "- 南山区：county，行政编码为 440305（广东省 / 深圳市）" in result["sql_rules"]
