from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from dataagent.agents.nl2sql.errors import NL2SQLError
from dataagent.agents.nl2sql.nodes.business_twin_perceptor import BusinessTwinPerceptorNode

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


class _ConfigManager:
    def get(self, _key: str, default: Any = None) -> Any:
        return default


def test_production_prompt_embeds_full_catalog_and_requires_bare_array() -> None:
    text = PROMPT_PATH.read_text(encoding="utf-8")
    catalog_names = re.findall(r"(?m)^- ([a-zA-Z0-9_*]+) \|", text)

    assert "{{CATALOG}}" not in text
    assert len(catalog_names) == 101
    assert len(set(catalog_names)) == 101
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
    assert (
        node._resolve_table_family_selection(
            {
                "family_name": "fact_dw1745159007_00000000000181c4",
                "granularity": "15min",
            },
            families,
        )
        == "fact_dw1745159007_00000000000181c4_metric_15min"
    )


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

    assert await node._select_business_id("查询华为手机保障次数") == "dw1745159016"


@pytest.mark.asyncio
async def test_async_business_id_selection_accepts_metric_in_unified_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())

    async def fake_execute(_context: dict[str, str], action: str = "") -> list[str]:
        return ["downlink_traffic", "downlink_duration", "county", "assurance_users"]

    monkeypatch.setattr(node, "execute_with_llm_json", fake_execute)

    assert await node._select_business_id("5月宜兴市保障用户网络下行速率提升百分比前20个") == ("dw1745159007")


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
