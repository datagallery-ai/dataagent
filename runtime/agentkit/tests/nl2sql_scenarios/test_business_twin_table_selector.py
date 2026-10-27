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
"""business_twin table-family selection: prompt contract, bitmask decoding, LLM chain.

Ported from the legacy
``tests/ut/agents/nl2sql/test_business_twin_perceptor_business_id.py``
(br_release_930). The perceptor variant no longer exists — the behavior lives
in :class:`BusinessTwinTableSelector` — so the family-selection tests drive
the selector's pipeline over a mock semantic catalog and a scripted model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from nl2sql.errors import DataAgentError
from scenarios.udn.business_twin import table_selector as table_selector_module
from scenarios.udn.business_twin.data.business_twin_business_id_catalog import BUSINESS_SCHEMAS
from scenarios.udn.business_twin.data.business_twin_business_id_selector import (
    BusinessTwinExtraction,
)
from scenarios.udn.business_twin.table_selector import BusinessTwinTableSelector

from nl2sql_scenarios.support import ScriptedModel, make_context, scripted_model, semantic_client

PACK_DIR = Path(table_selector_module.__file__).resolve().parent
SEMANTIC_SCENARIO = "business_twin"
PROMPT_PATH = PACK_DIR / "prompts" / "filter_business_twin_business_id_system.md"
USER_PROMPT_PATH = PROMPT_PATH.with_name("filter_business_twin_business_id_user.md")

TAI_LESS_TABLE = "fact_dw1745159007_00000000000181c4_metric_1h"  # no tai bit
TAI_TABLE = "fact_dw1745159007_000000000001b9c4_metric_1h"  # tai, gnb, cell_id
TAI_LESS_FAMILY = "fact_dw1745159007_00000000000181c4"
TAI_FAMILY = "fact_dw1745159007_000000000001b9c4"

_TAI_QUESTION = "查询最近三天小时粒度下，按TAI（tai字段）统计的保障用户保障前后网络下行速率提升百分比"
_RESOLUTION_QUESTION = "查询最近三天小时粒度下，按地市和TAI统计的保障用户占比最高分辨率次数分布"


def _extraction(**overrides: Any) -> BusinessTwinExtraction:
    defaults: dict[str, Any] = {
        "business_id": "dw1745159007",
        "metrics": frozenset({"downlink_traffic", "downlink_duration"}),
        "dimensions": frozenset({"guarantee_group", "tai"}),
        "granularity": "1h",
    }
    defaults.update(overrides)
    return BusinessTwinExtraction(**defaults)


def _column_extraction(columns: list[str]) -> str:
    """Scripted LLM reply: the extracted canonical columns as a bare JSON array."""
    return f"```json\n{json.dumps(columns)}\n```"


def _family_selection(family: str, granularity: str = "1h") -> str:
    """Scripted LLM reply: the model's table-family pick with explicit granularity."""
    payload = json.dumps(
        {"family_name": family, "granularity": granularity, "explicit_granularity": True}
    )
    return f"```json\n{payload}\n```"


def table_list_transport(
    tables: list[str],
) -> Callable[[httpx.Request], httpx.Response]:
    """Semantic-service mock serving only the table-list endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Semantic-Scenario") == SEMANTIC_SCENARIO, dict(request.headers)
        assert request.url.path.endswith("/advanced-search/table-list"), request.url.path
        return httpx.Response(
            200,
            json=[{f"business_twin.{name}": {"table_description": ""}} for name in tables],
        )

    return handler


def refusing_transport() -> Callable[[httpx.Request], httpx.Response]:
    """A semantic-service mock that fails the test on any request."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    return handler


@contextmanager
def table_selector(
    model: ScriptedModel, handler: Callable[[httpx.Request], httpx.Response]
) -> Iterator[BusinessTwinTableSelector]:
    """Yield a selector wired the way ``build_scenario`` wires it in the pack."""
    with semantic_client(handler) as client:
        yield BusinessTwinTableSelector(
            make_context("udn.business_twin", model, client),
            base=PACK_DIR,
            semantic_client=client.with_scenario(SEMANTIC_SCENARIO),
        )


async def select_table(model: ScriptedModel, tables: list[str], question: str) -> tuple[str, bool]:
    """Drive the family-selection pipeline over a mock table-list catalog."""
    with table_selector(model, table_list_transport(tables)) as selector:
        return await selector._select_table_by_business_family(question)


def test_production_prompt_embeds_full_catalog_and_requires_bare_array() -> None:
    """The production prompt embeds the whole field catalog and demands a bare JSON array."""
    text = PROMPT_PATH.read_text(encoding="utf-8")
    catalog_names = re.findall(r"(?m)^- ([a-zA-Z0-9_*]+) \|", text)
    catalog_fields = {
        field
        for schema in BUSINESS_SCHEMAS.values()
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
    for removed_key in ("metrics", "dimensions", "roles", "values", "subject", "unmapped_terms"):
        assert f'"{removed_key}"' not in text

    user_text = USER_PROMPT_PATH.read_text(encoding="utf-8")
    assert "只返回一个 JSON 字符串数组" in user_text


def test_railway_dimension_bit_is_exposed_on_its_table_family() -> None:
    """Bits 23-26 stay station, section, railway, and max-share resolution."""
    assert BusinessTwinTableSelector._decode_dimensions("0000000000800000") == ["crh_station_id"]
    assert BusinessTwinTableSelector._decode_dimensions("0000000001000000") == ["crh_section_id"]
    assert BusinessTwinTableSelector._decode_dimensions("0000000002000000") == ["crh_railway_id"]
    assert BusinessTwinTableSelector._decode_dimensions("0000000004000000") == ["most_resolution"]
    assert BusinessTwinTableSelector._decode_dimensions("0000000003000000") == [
        "crh_section_id",
        "crh_railway_id",
    ]
    assert (
        BusinessTwinTableSelector._decode_dimensions("0000000000801000") == ["gnb", "crh_station_id"]
    )
    assert BusinessTwinTableSelector._decode_dimensions("0000000000001000") == ["gnb"]
    assert BusinessTwinTableSelector._decode_dimensions("00000000000181c4") == [
        "guarantee_group",
        "custom_group",
        "app_id",
        "sub_app_id",
        "city",
        "county",
    ]

    families = BusinessTwinTableSelector._build_table_family_candidates(
        [
            {
                "bare_table_name": "fact_dw1745159031_0000000000001000_metric_1h",
                "business_id": "dw1745159031",
                "dimension_code": "0000000000001000",
                "granularity": "1h",
            },
            {
                "bare_table_name": "fact_dw1745159031_0000000002000000_metric_1h",
                "business_id": "dw1745159031",
                "dimension_code": "0000000002000000",
                "granularity": "1h",
            },
        ],
        ["dw1745159031"],
    )
    by_name = {family["family_name"]: family["dimensions"] for family in families}

    assert by_name["fact_dw1745159031_0000000002000000"] == ["crh_railway_id"]
    assert by_name["fact_dw1745159031_0000000000001000"] == ["gnb"]
    prompt = BusinessTwinTableSelector._format_table_family_prompt_context(families)
    assert "`crh_railway_id`（高铁线路）" in prompt
    railway_block = prompt.split("fact_dw1745159031_0000000002000000", 1)[1]
    assert "`crh_railway_id`" in railway_block.split("###", 1)[0]


def test_table_family_uses_granularity_to_table_mapping() -> None:
    families = BusinessTwinTableSelector._build_table_family_candidates(
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
    with table_selector(scripted_model(), refusing_transport()) as selector:
        assert selector._resolve_table(
            {
                "family_name": TAI_LESS_FAMILY,
                "granularity": "15min",
                "explicit_granularity": True,
            },
            families,
            _extraction(dimensions=frozenset({"guarantee_group"}), granularity=None),
        ) == ("fact_dw1745159007_00000000000181c4_metric_15min", True)


async def test_table_family_selection_rejects_llm_family_missing_required_dimension() -> None:
    """Regression: the model sometimes picked the tai-less family, the exact name
    that used to sit in the prompt's output example, and no check caught it."""
    model = scripted_model(
        _column_extraction(
            ["downlink_traffic", "downlink_duration", "guarantee_group", "1h_granularity", "tai"]
        ),
        _family_selection(TAI_LESS_FAMILY),
    )

    assert await select_table(model, [TAI_LESS_TABLE, TAI_TABLE], _TAI_QUESTION) == (TAI_TABLE, True)


async def test_table_family_selection_accepts_covering_llm_pick() -> None:
    model = scripted_model(
        _column_extraction(
            ["downlink_traffic", "downlink_duration", "guarantee_group", "1h_granularity", "tai"]
        ),
        _family_selection(TAI_FAMILY),
    )

    assert await select_table(model, [TAI_LESS_TABLE, TAI_TABLE], _TAI_QUESTION) == (TAI_TABLE, True)


async def test_table_family_selection_falls_back_when_granularity_missing_in_family() -> None:
    model = scripted_model(
        _column_extraction(
            ["downlink_traffic", "downlink_duration", "guarantee_group", "1h_granularity", "tai"]
        ),
        _family_selection(TAI_FAMILY, granularity="1d"),  # the family only has 1h tables
    )

    assert await select_table(model, [TAI_LESS_TABLE, TAI_TABLE], _TAI_QUESTION) == (TAI_TABLE, True)


async def test_table_family_ignores_extracted_dimension_no_family_carries() -> None:
    """Regression: the model sometimes extracts the ``most_resolution*_times`` metric as
    the bare ``most_resolution`` dimension, which maps to no dimension-code bit and is
    carried by no family. The uncoverable dimension must not disable the strict path."""
    model = scripted_model(
        _column_extraction(["most_resolution", "city", "tai"]),
        _family_selection(TAI_FAMILY),
    )

    assert await select_table(model, [TAI_LESS_TABLE, TAI_TABLE], _RESOLUTION_QUESTION) == (
        TAI_TABLE,
        True,
    )


async def test_table_family_misextracted_dimension_keeps_covering_llm_pick() -> None:
    """With only ``city`` extracted besides the mis-extracted ``most_resolution``, the
    uncoverable dimension used to force the fallback, whose fewest-extras heuristic
    picks the smaller tai-less family; the LLM's covering pick must win instead."""
    model = scripted_model(
        _column_extraction(["most_resolution", "city"]),
        _family_selection(TAI_FAMILY),
    )

    assert await select_table(model, [TAI_LESS_TABLE, TAI_TABLE], _RESOLUTION_QUESTION) == (
        TAI_TABLE,
        True,
    )


async def test_table_family_selection_relaxes_when_no_family_covers_required_dimensions() -> None:
    model = scripted_model(
        _column_extraction(
            ["downlink_traffic", "downlink_duration", "city", "tai", "1h_granularity"]
        ),
        _family_selection("fact_dw1745159007_00000000000ffff"),
    )

    # city only exists in the first family, tai only in the second one, so no
    # single family covers the required dimensions.
    assert await select_table(
        model,
        [TAI_LESS_TABLE, "fact_dw1745159007_0000000000000800_metric_1h"],
        _TAI_QUESTION,
    ) == ("fact_dw1745159007_0000000000000800_metric_1h", True)


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

    assert (
        BusinessTwinTableSelector._fallback_granularity(
            family, None, _extraction(granularity=target)
        )
        == expected
    )


async def test_business_id_selection_uses_bare_column_list() -> None:
    question = "查询华为手机保障次数"
    model = scripted_model(_column_extraction(["assurance_times", "term_brand"]))

    with table_selector(model, refusing_transport()) as selector:
        extraction = await selector._select_business_id(question)

    assert extraction.business_id == "dw1745159016"
    assert len(model.requests) == 1
    system_message, user_message = (message.content for message in model.requests[0])
    assert system_message == PROMPT_PATH.read_text(encoding="utf-8")
    assert question in user_message


async def test_business_id_selection_accepts_metric_in_unified_list() -> None:
    model = scripted_model(
        _column_extraction(["downlink_traffic", "downlink_duration", "county", "assurance_users"])
    )

    with table_selector(model, refusing_transport()) as selector:
        extraction = await selector._select_business_id(
            "5月宜兴市保障用户网络下行速率提升百分比前20个"
        )

    assert extraction.business_id == "dw1745159007"


async def test_business_id_selection_wraps_invalid_list() -> None:
    model = scripted_model('```json\n{"columns": ["downlink_traffic"]}\n```')

    with table_selector(model, refusing_transport()) as selector:
        with pytest.raises(DataAgentError, match="no valid result"):
            await selector._select_business_id("查询下行流量")
