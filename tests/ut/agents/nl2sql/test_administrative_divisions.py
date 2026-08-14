import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from dataagent.agents.nl2sql.nodes import business_twin_perceptor as business_twin_perceptor_module
from dataagent.agents.nl2sql.nodes.business_twin_perceptor import BusinessTwinPerceptorNode
from dataagent.agents.nl2sql.utils import administrative_divisions as administrative_divisions_module
from dataagent.agents.nl2sql.utils.administrative_divisions import (
    _load_administrative_division_index,
    format_administrative_division_rules,
    match_administrative_divisions,
)
from dataagent.utils.runtime_paths import dataagent_package_path

INDEX_PATH = dataagent_package_path(
    "agents",
    "nl2sql",
    "prompts",
    "perceptor",
    "china_administrative_division_aliases.json",
)
BUSINESS_TWIN_CONFIG_PATH = dataagent_package_path("agents", "nl2sql", "business_twin.yaml")


class _ConfigManager:
    def get(self, key: str, default: Any = None) -> Any:
        return default


def test_administrative_division_index_stays_with_perceptor_prompts() -> None:
    assert administrative_divisions_module._INDEX_PATH == INDEX_PATH
    assert "administrative_divisions:" not in BUSINESS_TWIN_CONFIG_PATH.read_text(encoding="utf-8")


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

    ninghe = next(item for item in divisions if (item["level"], item["code"]) == ("county", "120117"))
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


@pytest.mark.asyncio
async def test_business_twin_aprocess_appends_administrative_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())
    monkeypatch.setattr(node, "_load_prompt", lambda _name: "existing rule")
    monkeypatch.setattr(business_twin_perceptor_module, "schema_to_ddl", lambda *_args: "DDL")

    async def fake_schema_linking(_question: str):
        return {}, [], {}

    monkeypatch.setattr(node, "_business_twin_schema_linking", fake_schema_linking)
    state = {"question": "查询深圳市南山区流量"}

    result = await node._aprocess(state)

    assert result["sql_rules"].startswith(f"existing rule\n现在为{date.today().year}年")
    assert "\n\n## 行政区划匹配" in result["sql_rules"]
    assert "- 深圳市：city，行政编码为 440300（广东省）" in result["sql_rules"]
    assert "- 南山区：county，行政编码为 440305（广东省 / 深圳市）" in result["sql_rules"]
