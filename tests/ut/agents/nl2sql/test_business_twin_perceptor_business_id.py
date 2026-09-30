from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from dataagent.agents.nl2sql.errors import NL2SQLError
from dataagent.agents.nl2sql.nodes.business_twin_perceptor import BusinessTwinPerceptorNode
from dataagent.agents.nl2sql.utils.business_twin_business_id_selector import BusinessTwinExtraction
from dataagent.utils.runtime_paths import dataagent_package_path

PROMPT_PATH = (
    Path(__file__).resolve().parents[4]
    / "dataagent"
    / "agents"
    / "nl2sql"
    / "prompts"
    / "perceptor"
    / "filter_business_twin_business_id_system.md"
)
USER_PROMPT_PATH = PROMPT_PATH.with_name("filter_business_twin_business_id_user.md")
FAMILY_PROMPT_PATH = PROMPT_PATH.with_name("filter_business_twin_table_family_system.md")
CATALOG_PATH = dataagent_package_path(
    "agents",
    "nl2sql",
    "utils",
    "business_twin_business_id_catalog.json",
)


class _ConfigManager:
    def get(self, _key: str, default: Any = None) -> Any:
        return default


def test_production_prompt_embeds_full_catalog_and_requires_bare_array() -> None:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    catalog_names = re.findall(r"(?m)^- ([a-zA-Z0-9_*]+) \|", text)
    catalog_payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    catalog_fields = {
        field
        for schema in catalog_payload["business_schemas"].values()
        for field in (*schema["metrics"], *schema["dimensions"])
    }

    assert "{{CATALOG}}" not in text
    assert len(catalog_names) == 156
    assert len(set(catalog_names)) == 156
    assert set(catalog_names) == catalog_fields
    for granularity_field in ("1h_granularity", "1d_granularity", "15min_granularity"):
        assert granularity_field in catalog_names
    assert not re.findall(r"dw\d+", text)
    assert '["规范列名"]' in text
    for removed_key in (
        "metrics",
        "dimensions",
        "roles",
        "values",
        "subject",
        "unmapped_terms",
    ):
        assert f'"{removed_key}"' not in text

    user_text = USER_PROMPT_PATH.read_text(encoding="utf-8")
    assert "只返回一个 JSON 字符串数组" in user_text


def _extraction(**overrides: Any) -> BusinessTwinExtraction:
    defaults: dict[str, Any] = {
        "business_id": "dw1745159007",
        "metrics": frozenset({"downlink_traffic", "downlink_duration"}),
        "dimensions": frozenset({"guarantee_group", "tai"}),
        "granularity": "1h",
    }
    defaults.update(overrides)
    return BusinessTwinExtraction(**defaults)


def _tai_catalog() -> list[dict[str, str]]:
    return [
        {
            "bare_table_name": "fact_dw1745159007_00000000000181c4_metric_1h",
            "business_id": "dw1745159007",
            "dimension_code": "00000000000181c4",  # no tai
            "granularity": "1h",
        },
        {
            "bare_table_name": "fact_dw1745159007_000000000001b9c4_metric_1h",
            "business_id": "dw1745159007",
            "dimension_code": "000000000001b9c4",  # tai, gnb, cell_id
            "granularity": "1h",
        },
    ]


_TAI_QUESTION = "查询最近三天小时粒度下，按TAI（tai字段）统计的保障用户保障前后网络下行速率提升百分比"
_RESOLUTION_QUESTION = "查询最近三天小时粒度下，按地市和TAI统计的保障用户占比最高分辨率次数分布"


def test_table_family_uses_granularity_to_table_mapping() -> None:
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())
    families = node._build_table_family_candidates(
        [
            {
                "bare_table_name": "fact_dw1745159007_00000000000181c4_metric_1d",
                "business_id": "dw1745159007",
                "dimension_code": "00000000000181c4",
                "granularity": "1d",
            },
            {
                "bare_table_name": "fact_dw1745159007_00000000000181c4_metric_15min",
                "business_id": "dw1745159007",
                "dimension_code": "00000000000181c4",
                "granularity": "15min",
            },
        ],
        ["dw1745159007"],
    )

    assert families[0]["tables_by_granularity"] == {
        "15min": "fact_dw1745159007_00000000000181c4_metric_15min",
        "1d": "fact_dw1745159007_00000000000181c4_metric_1d",
    }
    assert node._resolve_table(
        {
            "family_name": "fact_dw1745159007_00000000000181c4",
            "granularity": "15min",
            "explicit_granularity": True,
        },
        families,
        _extraction(dimensions=frozenset({"guarantee_group"}), granularity=None),
    ) == ("fact_dw1745159007_00000000000181c4_metric_15min", True)


@pytest.mark.asyncio
async def test_table_family_selection_rejects_llm_family_missing_required_dimension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the model sometimes picked the tai-less family, the exact name
    that used to sit in the prompt's output example, and no check caught it."""
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())

    async def fake_execute(context: dict[str, str], action: str = "") -> Any:
        if action == "filter_business_twin_business_id_":
            return ["downlink_traffic", "downlink_duration", "guarantee_group", "1h_granularity", "tai"]
        assert action == "filter_business_twin_table_family_"
        return {
            "family_name": "fact_dw1745159007_00000000000181c4",
            "granularity": "1h",
            "explicit_granularity": True,
        }

    monkeypatch.setattr(node, "execute_with_llm_json", fake_execute)
    monkeypatch.setattr(node, "_full_table_catalog", lambda: _tai_catalog())

    assert await node._select_table_by_business_family(_TAI_QUESTION) == (
        "fact_dw1745159007_000000000001b9c4_metric_1h",
        True,
    )


@pytest.mark.asyncio
async def test_table_family_selection_accepts_covering_llm_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())

    async def fake_execute(context: dict[str, str], action: str = "") -> Any:
        if action == "filter_business_twin_business_id_":
            return ["downlink_traffic", "downlink_duration", "guarantee_group", "1h_granularity", "tai"]
        assert action == "filter_business_twin_table_family_"
        return {
            "family_name": "fact_dw1745159007_000000000001b9c4",
            "granularity": "1h",
            "explicit_granularity": True,
        }

    monkeypatch.setattr(node, "execute_with_llm_json", fake_execute)
    monkeypatch.setattr(node, "_full_table_catalog", lambda: _tai_catalog())

    assert await node._select_table_by_business_family(_TAI_QUESTION) == (
        "fact_dw1745159007_000000000001b9c4_metric_1h",
        True,
    )


@pytest.mark.asyncio
async def test_table_family_selection_falls_back_when_granularity_missing_in_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())

    async def fake_execute(context: dict[str, str], action: str = "") -> Any:
        if action == "filter_business_twin_business_id_":
            return ["downlink_traffic", "downlink_duration", "guarantee_group", "1h_granularity", "tai"]
        assert action == "filter_business_twin_table_family_"
        return {
            "family_name": "fact_dw1745159007_000000000001b9c4",
            "granularity": "1d",  # the family only has 1h tables
            "explicit_granularity": True,
        }

    monkeypatch.setattr(node, "execute_with_llm_json", fake_execute)
    monkeypatch.setattr(node, "_full_table_catalog", lambda: _tai_catalog())

    assert await node._select_table_by_business_family(_TAI_QUESTION) == (
        "fact_dw1745159007_000000000001b9c4_metric_1h",
        True,
    )


@pytest.mark.asyncio
async def test_table_family_ignores_extracted_dimension_no_family_carries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the model sometimes extracts the ``most_resolution*_times`` metric as
    the bare ``most_resolution`` dimension, which maps to no dimension-code bit and is
    carried by no family. The uncoverable dimension must not disable the strict path."""
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())

    async def fake_execute(context: dict[str, str], action: str = "") -> Any:
        if action == "filter_business_twin_business_id_":
            return ["most_resolution", "city", "tai"]
        assert action == "filter_business_twin_table_family_"
        return {
            "family_name": "fact_dw1745159007_000000000001b9c4",
            "granularity": "1h",
            "explicit_granularity": True,
        }

    monkeypatch.setattr(node, "execute_with_llm_json", fake_execute)
    monkeypatch.setattr(node, "_full_table_catalog", lambda: _tai_catalog())

    assert await node._select_table_by_business_family(_RESOLUTION_QUESTION) == (
        "fact_dw1745159007_000000000001b9c4_metric_1h",
        True,
    )


@pytest.mark.asyncio
async def test_table_family_misextracted_dimension_keeps_covering_llm_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With only ``city`` extracted besides the mis-extracted ``most_resolution``, the
    uncoverable dimension used to force the fallback, whose fewest-extras heuristic
    picks the smaller tai-less family; the LLM's covering pick must win instead."""
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())

    async def fake_execute(context: dict[str, str], action: str = "") -> Any:
        if action == "filter_business_twin_business_id_":
            return ["most_resolution", "city"]
        assert action == "filter_business_twin_table_family_"
        return {
            "family_name": "fact_dw1745159007_000000000001b9c4",
            "granularity": "1h",
            "explicit_granularity": True,
        }

    monkeypatch.setattr(node, "execute_with_llm_json", fake_execute)
    monkeypatch.setattr(node, "_full_table_catalog", lambda: _tai_catalog())

    assert await node._select_table_by_business_family(_RESOLUTION_QUESTION) == (
        "fact_dw1745159007_000000000001b9c4_metric_1h",
        True,
    )


@pytest.mark.asyncio
async def test_table_family_selection_relaxes_when_no_family_covers_required_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())

    async def fake_execute(context: dict[str, str], action: str = "") -> Any:
        if action == "filter_business_twin_business_id_":
            return ["downlink_traffic", "downlink_duration", "city", "tai", "1h_granularity"]
        assert action == "filter_business_twin_table_family_"
        return {
            "family_name": "fact_dw1745159007_00000000000ffff",
            "granularity": "1h",
            "explicit_granularity": True,
        }

    monkeypatch.setattr(node, "execute_with_llm_json", fake_execute)
    # city only exists in the first family, tai only in the second one, so no
    # single family covers the required dimensions.
    monkeypatch.setattr(
        node,
        "_full_table_catalog",
        lambda: [
            *_tai_catalog()[:1],
            {
                "bare_table_name": "fact_dw1745159007_0000000000000800_metric_1h",
                "business_id": "dw1745159007",
                "dimension_code": "0000000000000800",  # tai, no city
                "granularity": "1h",
            },
        ],
    )

    assert await node._select_table_by_business_family(_TAI_QUESTION) == (
        "fact_dw1745159007_0000000000000800_metric_1h",
        True,
    )


@pytest.mark.parametrize(
    ("available", "target", "expected"),
    [
        ({"5min": "t5", "1h": "t1"}, "15min", "5min"),  # closest finer
        ({"1h": "t1", "1d": "td"}, "15min", "1h"),  # closest coarser
        ({"15min": "t15"}, "1h", "15min"),  # only a finer one exists
        ({"1d": "td"}, None, "1d"),  # no target at all: finest available
    ],
)
def test_fallback_granularity_prefers_explicit_then_closest(
    available: dict[str, str], target: str | None, expected: str
) -> None:
    family = {"tables_by_granularity": available}

    assert BusinessTwinPerceptorNode._fallback_granularity(family, None, _extraction(granularity=target)) == expected


@pytest.mark.asyncio
async def test_async_business_id_selection_uses_bare_column_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())

    async def fake_execute(context: dict[str, str], action: str = "") -> list[str]:
        assert context == {"question": "查询华为手机保障次数"}
        assert action == "filter_business_twin_business_id_"
        return ["assurance_times", "term_brand"]

    monkeypatch.setattr(node, "execute_with_llm_json", fake_execute)

    assert (await node._select_business_id("查询华为手机保障次数")).business_id == "dw1745159016"


@pytest.mark.asyncio
async def test_async_business_id_selection_accepts_metric_in_unified_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())

    async def fake_execute(_context: dict[str, str], action: str = "") -> list[str]:
        return ["downlink_traffic", "downlink_duration", "county", "assurance_users"]

    monkeypatch.setattr(node, "execute_with_llm_json", fake_execute)

    assert (
        await node._select_business_id("5月宜兴市保障用户网络下行速率提升百分比前20个")
    ).business_id == "dw1745159007"


@pytest.mark.asyncio
async def test_async_business_id_selection_wraps_invalid_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())

    async def fake_execute(_context: dict[str, str], action: str = "") -> dict[str, list[str]]:
        return {"columns": ["downlink_traffic"]}

    monkeypatch.setattr(node, "execute_with_llm_json", fake_execute)

    with pytest.raises(NL2SQLError, match="no valid result"):
        await node._select_business_id("查询下行流量")
