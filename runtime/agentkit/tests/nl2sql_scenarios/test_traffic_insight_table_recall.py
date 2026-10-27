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
"""Traffic Insight table recall: pure helpers plus the selector's service plumbing.

Ported from the legacy ``tests/ut/agents/nl2sql/test_traffic_insight_perceptor_recall.py``
(br_release_930). The pure recall / clustering / convergence / parsing helpers are
exercised against ``scenarios.udn.traffic_insight.data.traffic_insight_table_recall``
directly; the semantic-service plumbing (field-EQ pagination, hybrid table-columns
batches, columns-info value supplement) and the two-LLM selection pipeline run against
``TrafficInsightTableSelector`` with a fake semantic client and a scripted model —
no network, no real LLM.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from nl2sql.config_manager import NL2SQLConfig
from nl2sql.constants import DEFAULT_NL2SQL_SEMANTIC_TABLE_COLUMNS_LIMIT
from nl2sql.errors import DataAgentError
from nl2sql.hooks import HOOK_POINTS, PERCEPTOR_SCHEMA_CONTEXT, load_scenario
from scenarios.udn.traffic_insight import table_selector as traffic_insight_pack
from scenarios.udn.traffic_insight.data.traffic_insight_field_normalizer import (
    assert_need_fields,
    normalize_traffic_insight_fields,
)
from scenarios.udn.traffic_insight.data.traffic_insight_table_recall import (
    build_families_from_tables,
    build_field_index_from_hits,
    enrich_families_with_columns,
    enrich_schema_example_values_from_columns_info,
    format_traffic_insight_table_family_prompt_context,
    normalize_example_values,
    normalize_family_table_description,
    select_families_by_max_need_hits,
    select_families_by_min_extra_fields,
    select_families_by_min_extra_fields_converged,
    tables_from_field_index,
    tables_missing_from_hybrid_columns,
)
from scenarios.udn.traffic_insight.table_selector import TrafficInsightTableSelector

from nl2sql_scenarios.support import make_context, scripted_model

_BASE = Path(traffic_insight_pack.__file__).resolve().parent
"""The traffic_insight pack directory (prompts, sql rules) the tests assemble against."""


def _llm_json(payload: str) -> str:
    """Wrap a JSON payload in the fenced block the shared LLM JSON parser expects."""
    return f"```json\n{payload}\n```"


class _FakeSemanticClient:
    """Semantic-service stand-in; tests replace methods per case."""

    def search_basic(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"entities": []}

    def hybrid_table_columns(self, tables: list[str]) -> list[dict[str, Any]]:
        return []

    def get_table_columns_info(self, table_name: str, *, limit: int = 1000) -> dict[str, Any]:
        return {}


def _make_selector(
    model: BaseChatModel | None = None,
    client: _FakeSemanticClient | None = None,
    **overrides: Any,
) -> TrafficInsightTableSelector:
    """A selector bound to database ``db``; recall knobs pass through as overrides."""
    if client is None:
        client = _FakeSemanticClient()
    ctx = make_context("udn.traffic_insight", model or scripted_model(), client, db_id="db")
    return TrafficInsightTableSelector(ctx, base=_BASE, semantic_client=client, **overrides)


def _column_search(tables: list[tuple[str, str]]) -> dict[str, Any]:
    """A search/basic data_column payload listing the given (db, table) pairs."""
    return {
        "entities": [
            {
                "attributes": {
                    "db_name_en": db,
                    "table_name_en": table,
                    "column_name_en": "ignored",
                }
            }
            for db, table in tables
        ]
    }


def _field_from_payload(payload: dict[str, Any]) -> str:
    """Extract the queried column name from a search/basic entityFilters payload."""
    filters = payload["entityFilters"]
    if "criterion" in filters:
        for item in filters["criterion"]:
            if item.get("attributeName") == "column_name_en":
                return str(item["attributeValue"])
    return str(filters["attributeValue"])


def _hybrid_payload(db: str, table: str, columns: list[str]) -> dict[str, Any]:
    """One hybrid/table-columns item with string-typed, description-less columns."""
    return {
        "db": db,
        "table": table,
        "description": "",
        "columns": [
            {"columnNameEn": name, "description": "", "valueType": "string"} for name in columns
        ],
    }


@pytest.mark.parametrize(
    ("scenario", "expected_selector"),
    [
        ("udn.business_twin", "BusinessTwinTableSelector"),
        ("udn.traffic_insight", "TrafficInsightTableSelector"),
    ],
)
def test_scenario_config_selects_the_pack_table_selector(
    scenario: str,
    expected_selector: str,
) -> None:
    """Legacy ``_perceptor_class`` routing is now config ``scenario`` → pack handlers."""
    ctx = make_context(scenario, scripted_model("[]"), None)
    handlers = load_scenario(ctx)
    assert set(handlers) == HOOK_POINTS
    handler = handlers[PERCEPTOR_SCHEMA_CONTEXT]
    # The loader imports packs under a private per-root namespace, so the wired
    # selector's class is not the same module object as a plain ``scenarios.*``
    # import; pin the defining module and class name instead of class identity.
    selector = type(handler.__self__)
    assert selector.__name__ == expected_selector
    assert selector.__module__.endswith(f"{scenario}.table_selector")


def test_no_scenario_config_keeps_the_default_perceptor_flow() -> None:
    """Without ``scenario`` no pack is assembled, so the base perceptor schema flow runs."""
    config = NL2SQLConfig(
        {
            "model": "primary",
            "database": {"db_id": "db", "dialect": "postgres", "engine": "postgres"},
        }
    )
    assert config.scenario is None


def test_normalize_allows_metrics_only() -> None:
    """Metrics-only need sets pass the need-field assertion (no dimension required)."""
    need = normalize_traffic_insight_fields(["downlink_volume", "subs_count"])
    assert need["need_d"] == set()
    assert need["need_m"] == {"downlink_volume", "subs_count"}
    assert_need_fields(need["need_d"], need["need_m"])


def test_field_index_excludes_dim_and_keeps_all_hits() -> None:
    """Excluded dimension tables never enter the index; every other hit is kept."""
    index = build_field_index_from_hits(
        {
            "cell": {"db.appcate_cell_1h", "db.other_1h", "db.dim_cell"},
            "appcate": {"db.appcate_cell_1h", "db.appcate_city_1h"},
            "downlink_volume": {"db.appcate_cell_1h", "db.appcate_city_1h"},
        },
        need_d={"cell", "appcate"},
        need_m={"downlink_volume"},
    )
    tables = tables_from_field_index(index)
    assert "db.dim_cell" not in tables
    assert set(tables) == {"db.appcate_cell_1h", "db.appcate_city_1h", "db.other_1h"}
    assert index["db.appcate_cell_1h"] == {"cell", "appcate", "downlink_volume"}


def test_select_families_keeps_max_need_hit_tier_with_ties() -> None:
    """Only the highest need-hit tier survives; ties within it are all kept."""
    index = build_field_index_from_hits(
        {
            "cell": {"db.appcate_cell_1h", "db.cell_city_1h", "db.wide_cell_appcate_1h"},
            "appcate": {"db.appcate_city_1h", "db.appcate_cell_1h", "db.wide_cell_appcate_1h"},
            "downlink_volume": {
                "db.appcate_cell_1h",
                "db.appcate_city_1h",
                "db.wide_cell_appcate_1h",
            },
            "subs_count": {"db.appcate_cell_1h", "db.wide_cell_appcate_1h"},
        },
        need_d={"cell", "appcate"},
        need_m={"downlink_volume", "subs_count"},
    )
    # appcate_cell / wide_cell_appcate hit 4; appcate_city hits 2; cell_city hits 1
    families = build_families_from_tables(tables_from_field_index(index))
    selected = select_families_by_max_need_hits(families, index)
    names = {family["family_name"] for family in selected}
    assert names == {"appcate_cell", "wide_cell_appcate"}


def test_enrich_keeps_partial_families_without_ranking() -> None:
    """Enrich attaches catalog-split dims/metrics without dropping or ranking families."""
    families = build_families_from_tables(["db.appcate_cell_1h"])
    columns_map = {
        "appcate_cell_1h": {
            "description": "",
            "columns": {
                "cell": {"description": "", "value_type": "", "example_values": ""},
                "appcate": {"description": "", "value_type": "", "example_values": ""},
                "downlink_volume": {"description": "", "value_type": "", "example_values": ""},
            },
            "qualified_name": "db.appcate_cell_1h",
            "bare_name": "appcate_cell_1h",
        },
        "db.appcate_cell_1h": {
            "description": "",
            "columns": {
                "cell": {"description": "", "value_type": "", "example_values": ""},
                "appcate": {"description": "", "value_type": "", "example_values": ""},
                "downlink_volume": {"description": "", "value_type": "", "example_values": ""},
            },
            "qualified_name": "db.appcate_cell_1h",
            "bare_name": "appcate_cell_1h",
        },
    }
    enriched = enrich_families_with_columns(families, columns_map)
    assert len(enriched) == 1
    assert enriched[0]["family_name"] == "appcate_cell"
    assert "cell" in enriched[0]["dimensions"]
    assert "downlink_volume" in enriched[0]["metrics"]


def test_format_prompt_context_dims_and_metrics_with_hybrid_description() -> None:
    """The LLM₂ context renders a deduped dim+metric legend and per-family clusters."""
    families = [
        {
            "family_name": "appcate_cell",
            "dimensions": ["cell", "appcate"],
            "metrics": ["downlink_volume"],
            "available_granularities": ["1h"],
            "column_meta": {
                "description": "按 1 小时粒度存储基于应用大类、小区维度的下行流量与用户数统计指标数据",
                "columns": {
                    "cell": {"description": "小区标识", "value_type": "string"},
                    "appcate": {"description": "应用大类", "value_type": "string"},
                    "downlink_volume": {"description": "下行流量", "value_type": "long"},
                },
            },
        },
        {
            "family_name": "appcate_cell_network_type",
            "dimensions": ["cell", "appcate", "network_type"],
            "metrics": ["downlink_volume", "subs_count"],
            "available_granularities": ["1h"],
            "column_meta": {
                "description": "按 1 小时粒度存储基于应用大类、小区、网络类型维度的下行流量与用户数统计指标数据",
                "columns": {
                    "cell": {"description": "小区标识", "value_type": "string"},
                    "appcate": {"description": "", "value_type": "string"},
                    "network_type": {"description": "网络类型", "value_type": "string"},
                    "downlink_volume": {"description": "下行流量", "value_type": "long"},
                    "subs_count": {"description": "", "value_type": "long"},
                },
            },
        },
    ]
    text = format_traffic_insight_table_family_prompt_context(families)
    assert "## 维度和指标说明" in text
    assert "- `cell`（小区标识）" in text
    assert "- `appcate`（应用大类）" in text
    assert "- `downlink_volume`（下行流量）" in text
    assert "- `network_type`（网络类型）" in text
    assert "- `subs_count`" in text
    assert "- 表簇说明：存储基于应用大类、小区维度的下行流量与用户数统计指标数据" in text
    assert "- 表簇说明：存储基于应用大类、小区、网络类型维度的下行流量与用户数统计指标数据" in text
    assert "（）" not in text
    assert "## 维度说明" not in text


def test_normalize_family_table_description_strips_granularity_prefix() -> None:
    """Descriptions keep only the shared business suffix after the granularity clause."""
    suffix = "存储基于接入制式、小区维度的流量统计指标数据，涵盖上下行流量、连接数、用户数等核心指标"
    assert normalize_family_table_description(f"按 5 分钟粒度{suffix}") == suffix
    assert normalize_family_table_description(f"按 1 小时粒度{suffix}") == suffix
    assert normalize_family_table_description(f"按 1 天粒度{suffix}") == suffix
    assert normalize_family_table_description("") == ""


def test_select_families_by_min_extra_fields_top2_tiers() -> None:
    """top_n=2 keeps the two smallest extra tiers, ties included."""
    need_d = {"cell", "appcate"}
    need_m = {"downlink_volume"}
    families = [
        {
            "family_name": "narrow",
            "dimensions": ["cell", "appcate"],
            "metrics": ["downlink_volume"],
        },
        {
            "family_name": "wide_one_extra_dim",
            "dimensions": ["cell", "appcate", "city"],
            "metrics": ["downlink_volume"],
        },
        {
            "family_name": "also_narrow",
            "dimensions": ["appcate", "cell"],
            "metrics": ["downlink_volume"],
        },
        {
            "family_name": "wide_one_extra_metric",
            "dimensions": ["cell", "appcate"],
            "metrics": ["downlink_volume", "subs_count"],
        },
        {
            "family_name": "wider_two_extras",
            "dimensions": ["cell", "appcate", "city"],
            "metrics": ["downlink_volume", "subs_count"],
        },
    ]
    # extras: narrow/also_narrow=0; wide_*=1; wider_two_extras=2 → top2 tiers {0,1}
    kept = select_families_by_min_extra_fields(families, need_d=need_d, need_m=need_m, top_n=2)
    assert {family["family_name"] for family in kept} == {
        "narrow",
        "also_narrow",
        "wide_one_extra_dim",
        "wide_one_extra_metric",
    }


def test_select_families_by_min_extra_fields_top5_keeps_all_distinct_tiers() -> None:
    """top_n=5 keeps every family when fewer than five distinct extra tiers exist."""
    need_d = {"cell", "appcate"}
    need_m = {"downlink_volume"}
    families = [
        {
            "family_name": "narrow",
            "dimensions": ["cell", "appcate"],
            "metrics": ["downlink_volume"],
        },
        {
            "family_name": "wide_one_extra_dim",
            "dimensions": ["cell", "appcate", "city"],
            "metrics": ["downlink_volume"],
        },
        {
            "family_name": "also_narrow",
            "dimensions": ["appcate", "cell"],
            "metrics": ["downlink_volume"],
        },
        {
            "family_name": "wide_one_extra_metric",
            "dimensions": ["cell", "appcate"],
            "metrics": ["downlink_volume", "subs_count"],
        },
        {
            "family_name": "wider_two_extras",
            "dimensions": ["cell", "appcate", "city"],
            "metrics": ["downlink_volume", "subs_count"],
        },
    ]
    kept = select_families_by_min_extra_fields(families, need_d=need_d, need_m=need_m, top_n=5)
    assert {family["family_name"] for family in kept} == {
        "narrow",
        "also_narrow",
        "wide_one_extra_dim",
        "wide_one_extra_metric",
        "wider_two_extras",
    }


def _families_with_extra(extra: int, count: int, *, prefix: str = "family") -> list[dict[str, Any]]:
    """``count`` families whose dimensions carry ``extra`` fields beyond the need set."""
    families: list[dict[str, Any]] = []
    for idx in range(count):
        dims = ["cell", "appcate"]
        metrics = ["downlink_volume"]
        for _ in range(extra):
            dims.append(f"extra_dim_{idx}")
        families.append(
            {
                "family_name": f"{prefix}_{extra}_{idx:03d}",
                "dimensions": dims,
                "metrics": metrics,
            }
        )
    return families


def test_select_families_by_min_extra_fields_converged_reduces_top_n() -> None:
    """Over-limit results converge by shrinking top_n until the limit is met."""
    need_d = {"cell", "appcate"}
    need_m = {"downlink_volume"}
    families = [
        *_families_with_extra(0, 120, prefix="tier0"),
        *_families_with_extra(1, 120, prefix="tier1"),
    ]
    kept = select_families_by_min_extra_fields_converged(
        families,
        need_d=need_d,
        need_m=need_m,
        top_n=5,
        limit=200,
    )
    assert len(kept) == 120
    assert all(family["family_name"].startswith("tier0_") for family in kept)


def test_select_families_by_min_extra_fields_converged_truncates_at_top1() -> None:
    """When even top1 exceeds the limit, the sorted prefix of the lowest tier is kept."""
    need_d = {"cell", "appcate"}
    need_m = {"downlink_volume"}
    families = _families_with_extra(0, 250, prefix="tier0")
    kept = select_families_by_min_extra_fields_converged(
        families,
        need_d=need_d,
        need_m=need_m,
        top_n=5,
        limit=200,
    )
    assert len(kept) == 200
    assert [family["family_name"] for family in kept] == sorted(f["family_name"] for f in kept)


def test_tables_missing_from_hybrid_columns() -> None:
    """Missing detection matches both qualified and bare keys of the columns map."""
    columns_map = {"db.a_1h": {"columns": {}}, "b_1h": {"columns": {}}}
    assert tables_missing_from_hybrid_columns(["db.a_1h", "db.b_1h", "db.c_1h"], columns_map) == [
        "db.c_1h"
    ]


def test_normalize_example_values() -> None:
    """``a:b|c:d`` value descriptions become ``a=b|c=d``; empty stays empty."""
    assert normalize_example_values("a:b|c:d") == "a=b|c=d"
    assert normalize_example_values("") == ""
    assert normalize_example_values(None) == ""


def test_enrich_example_values_supplements_without_overwrite() -> None:
    """columns-info supplements only empty example values; never overwrites or adds columns."""
    schema = {
        "appcate_cell_1h": {
            "description": "hybrid表描述",
            "columns": {
                "network_type": {
                    "description": "hybrid维描述",
                    "value_type": "string",
                    "example_values": "",
                },
                "downlink_volume": {
                    "description": "hybrid指标",
                    "value_type": "bigint",
                    "example_values": "keep=已有",
                },
            },
        }
    }
    info = {
        "db.appcate_cell_1h.network_type": {
            "column_short_description": "应被忽略的列描述",
            "value_type": "ignored",
            "value_description": "4G:4G网络|5G:5G网络",
        },
        "db.appcate_cell_1h.downlink_volume": {
            "column_short_description": "应被忽略",
            "value_type": "ignored",
            "value_description": "should:not_overwrite",
        },
        "db.appcate_cell_1h.only_in_info": {
            "column_short_description": "不应新增",
            "value_type": "string",
            "value_description": "x:y",
        },
    }
    enriched = enrich_schema_example_values_from_columns_info(schema, "db.appcate_cell_1h", info)
    cols = enriched["appcate_cell_1h"]["columns"]
    assert enriched["appcate_cell_1h"]["description"] == "hybrid表描述"
    assert cols["network_type"]["description"] == "hybrid维描述"
    assert cols["network_type"]["value_type"] == "string"
    assert cols["network_type"]["example_values"] == "4G=4G网络|5G=5G网络"
    assert cols["downlink_volume"]["example_values"] == "keep=已有"
    assert "only_in_info" not in cols


def test_schema_for_selected_table_hybrid_plus_values() -> None:
    """The DDL schema comes from hybrid columns; columns-info only fills example values."""
    client = _FakeSemanticClient()

    def fake_info(table_name: str, *, limit: int = 1000) -> dict[str, Any]:
        assert table_name == "db.cell_1h"
        return {
            "db.cell_1h.cell": {
                "column_short_description": "info侧描述应忽略",
                "value_type": "ignored",
                "value_description": "A:小区A",
            },
            "db.cell_1h.extra_only_info": {
                "column_short_description": "不应进schema",
                "value_type": "string",
                "value_description": "x:y",
            },
        }

    def fake_hybrid(tables: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "db": "db",
                "table": "cell_1h",
                "description": "小区小时事实表",
                "columns": [
                    {
                        "columnNameEn": "cell",
                        "description": "hybrid小区描述",
                        "valueType": "string",
                    }
                ],
            }
        ]

    client.get_table_columns_info = fake_info
    client.hybrid_table_columns = fake_hybrid
    selector = _make_selector(client=client)

    schema = selector._schema_for_selected_table("db.cell_1h")
    assert schema["cell_1h"]["description"] == "小区小时事实表"
    assert schema["cell_1h"]["columns"]["cell"]["description"] == "hybrid小区描述"
    assert schema["cell_1h"]["columns"]["cell"]["value_type"] == "string"
    assert schema["cell_1h"]["columns"]["cell"]["example_values"] == "A=小区A"
    assert "extra_only_info" not in schema["cell_1h"]["columns"]


def test_columns_info_single_call_no_offset_loop() -> None:
    """columns-info uses the single-call contract: one call, default limit, no offset loop."""
    client = _FakeSemanticClient()
    calls: list[dict[str, Any]] = []

    def fake_info(table_name: str, *, limit: int = 1000) -> dict[str, Any]:
        # No ``offset`` parameter: passing one would fail this signature outright.
        calls.append({"table_name": table_name, "limit": limit})
        return {
            "db.wide_1h.tail": {
                "column_short_description": "尾列应忽略",
                "value_type": "ignored",
                "value_description": "x:y",
            },
            "db.wide_1h.col_0": {
                "column_short_description": "",
                "value_type": "string",
                "value_description": "",
            },
        }

    def fake_hybrid(tables: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "db": "db",
                "table": "wide_1h",
                "description": "宽表",
                "columns": [
                    {"columnNameEn": "tail", "description": "hybrid尾列", "valueType": "string"},
                    {"columnNameEn": "col_0", "description": "c0", "valueType": "string"},
                ],
            }
        ]

    client.get_table_columns_info = fake_info
    client.hybrid_table_columns = fake_hybrid
    selector = _make_selector(client=client)

    schema = selector._schema_for_selected_table("db.wide_1h")
    assert calls == [
        {"table_name": "db.wide_1h", "limit": DEFAULT_NL2SQL_SEMANTIC_TABLE_COLUMNS_LIMIT}
    ]
    assert schema["wide_1h"]["description"] == "宽表"
    assert set(schema["wide_1h"]["columns"]) == {"tail", "col_0"}
    assert schema["wide_1h"]["columns"]["tail"]["description"] == "hybrid尾列"
    assert schema["wide_1h"]["columns"]["tail"]["example_values"] == "x=y"


def test_hybrid_multi_batch_fetches_all_and_retries_missing() -> None:
    """Hybrid fetch batches by size and retries tables missing after the first pass."""
    client = _FakeSemanticClient()
    calls: list[list[str]] = []

    def fake_hybrid(tables: list[str]) -> list[dict[str, Any]]:
        calls.append(list(tables))
        out: list[dict[str, Any]] = []
        for qualified in tables:
            bare = qualified.rsplit(".", 1)[-1]
            city_requests = sum(1 for chunk in calls for name in chunk if name.endswith("city_1h"))
            # Omit city_1h on its first request to force a retry pass.
            if bare == "city_1h" and city_requests == 1:
                continue
            out.append(_hybrid_payload("db", bare, ["cell"]))
        return out

    client.hybrid_table_columns = fake_hybrid
    selector = _make_selector(client=client, hybrid_batch_size=1)

    result = selector._hybrid_columns_for_tables(["db.cell_1h", "db.city_1h"])
    assert "db.cell_1h" in result or "cell_1h" in result
    assert "db.city_1h" in result or "city_1h" in result
    assert len(calls) >= 3  # 2 first-pass batches + 1 retry for the missing city table


def test_hybrid_missing_after_retry_raises() -> None:
    """A table still missing after the retry pass fails the fetch (never dropped silently)."""
    client = _FakeSemanticClient()

    def fake_hybrid(tables: list[str]) -> list[dict[str, Any]]:
        return [_hybrid_payload("db", "cell_1h", ["cell"])]

    client.hybrid_table_columns = fake_hybrid
    selector = _make_selector(client=client)

    with pytest.raises(DataAgentError, match="did not return all requested tables"):
        selector._hybrid_columns_for_tables(["db.cell_1h", "db.missing_1h"])


def test_column_eq_pagination_streams_into_index() -> None:
    """EQ pages stream into the inverted index; a short page ends pagination."""
    client = _FakeSemanticClient()
    offsets: list[int] = []

    def fake_search_basic(payload: dict[str, Any]) -> dict[str, Any]:
        offsets.append(int(payload["offset"]))
        offset = int(payload["offset"])
        pages = {
            0: [("db", "t1_1h"), ("db", "t2_1h")],
            2: [("db", "t3_1h")],  # short page ends pagination
        }
        return _column_search(pages.get(offset, []))

    client.search_basic = fake_search_basic
    selector = _make_selector(client=client, column_eq_page_size=2)

    table_to_fields: dict[str, set[str]] = {}
    hit_tables = selector._stream_field_eq_into_index("cell", table_to_fields)
    assert offsets == [0, 2]
    assert hit_tables == 3
    assert table_to_fields == {
        "db.t1_1h": {"cell"},
        "db.t2_1h": {"cell"},
        "db.t3_1h": {"cell"},
    }


async def test_select_table_max_need_hit_family_and_resolves() -> None:
    """Only the max need-hit family is hybrid-fetched; LLM₂ picks family + granularity."""
    client = _FakeSemanticClient()
    model = scripted_model(
        _llm_json('["cell", "appcate", "downlink_volume", "subs_count"]'),
        _llm_json('{"family_name": "appcate_cell", "granularity": "1h"}'),
    )
    selector = _make_selector(model=model, client=client)
    hybrid_requests: list[list[str]] = []

    field_tables = {
        "cell": _column_search([("db", "appcate_cell_1h"), ("db", "cell_city_1h")]),
        "appcate": _column_search([("db", "appcate_city_1h")]),
        "downlink_volume": _column_search([("db", "appcate_cell_1h"), ("db", "appcate_city_1h")]),
        "subs_count": _column_search([("db", "appcate_cell_1h")]),
    }

    def fake_search_basic(payload: dict[str, Any]) -> dict[str, Any]:
        field = _field_from_payload(payload)
        assert payload["entityFilters"]["condition"] == "AND"
        assert any(
            item.get("attributeName") == "db_name_en" and item.get("attributeValue") == "db"
            for item in payload["entityFilters"]["criterion"]
        )
        return field_tables[field]

    def fake_hybrid(tables: list[str]) -> list[dict[str, Any]]:
        hybrid_requests.append(list(tables))
        out: list[dict[str, Any]] = []
        for qualified in tables:
            bare = qualified.rsplit(".", 1)[-1]
            cols = ["cell", "appcate", "downlink_volume", "subs_count", "time"]
            if bare == "appcate_city_1h":
                cols = ["appcate", "city", "downlink_volume"]
            out.append(_hybrid_payload("db", bare, cols))
        return out

    client.search_basic = fake_search_basic
    client.hybrid_table_columns = fake_hybrid

    table, explicit_granularity = await selector._select_traffic_insight_table(
        "各小区应用大类下行流量和用户数按小时"
    )
    assert table in {"appcate_cell_1h", "db.appcate_cell_1h"}
    assert explicit_granularity is False  # LLM₂ omitted explicit_granularity → treated as derived
    # Only the max need-hit family (appcate_cell hits 3) is hybrid-fetched; lower tiers dropped.
    requested_bares = {name.rsplit(".", 1)[-1] for chunk in hybrid_requests for name in chunk}
    assert requested_bares == {"appcate_cell_1h"}


async def test_select_table_min_extra_top5_keeps_more_tiers() -> None:
    """Max-hit families with extra tiers 0, 1, and beyond all reach LLM₂ under default top5."""
    client = _FakeSemanticClient()
    model = scripted_model(
        _llm_json('["cell", "appcate", "downlink_volume"]'),
        _llm_json('{"family_name": "appcate_cell", "granularity": "1h"}'),
    )
    selector = _make_selector(model=model, client=client)

    hits = [
        ("db", "appcate_cell_1h"),
        ("db", "wide_appcate_cell_1h"),
        ("db", "wider_appcate_cell_1h"),
    ]
    field_tables = {
        field: _column_search(hits) for field in ("cell", "appcate", "downlink_volume")
    }

    def fake_search_basic(payload: dict[str, Any]) -> dict[str, Any]:
        return field_tables[_field_from_payload(payload)]

    def fake_hybrid(tables: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for qualified in tables:
            bare = qualified.rsplit(".", 1)[-1]
            if bare.startswith("wider_"):
                cols = ["cell", "appcate", "network_type", "ip_version",
                        "downlink_volume", "subs_count"]
            elif bare.startswith("wide_"):
                cols = ["cell", "appcate", "network_type", "downlink_volume"]
            else:
                cols = ["cell", "appcate", "downlink_volume"]
            out.append(_hybrid_payload("db", bare, cols))
        return out

    client.search_basic = fake_search_basic
    client.hybrid_table_columns = fake_hybrid

    table, _ = await selector._select_traffic_insight_table("各小区应用大类下行流量")
    assert "appcate_cell_1h" in table
    # Exactly one family-selection LLM call; its rendered user prompt carries every candidate.
    assert len(model.requests) == 2
    llm2_user_prompt = model.requests[1][-1].content
    assert "`appcate_cell`" in llm2_user_prompt
    assert "`wide_appcate_cell`" in llm2_user_prompt
    assert "`wider_appcate_cell`" in llm2_user_prompt


async def test_select_table_need_d_empty_continues_with_metrics() -> None:
    """Metrics-only field sets skip the need_d requirement and still select a table."""
    client = _FakeSemanticClient()
    model = scripted_model(
        _llm_json('["downlink_volume"]'),
        _llm_json('{"family_name": "appcate_cell", "granularity": "1h"}'),
    )
    selector = _make_selector(model=model, client=client)

    def fake_search_basic(payload: dict[str, Any]) -> dict[str, Any]:
        assert _field_from_payload(payload) == "downlink_volume"
        return _column_search([("db", "appcate_cell_1h"), ("db", "dim_metric")])

    def fake_hybrid(tables: list[str]) -> list[dict[str, Any]]:
        return [_hybrid_payload("db", "appcate_cell_1h", ["downlink_volume", "cell"])]

    client.search_basic = fake_search_basic
    client.hybrid_table_columns = fake_hybrid

    table, _ = await selector._select_traffic_insight_table("查一下行流量")
    assert "appcate_cell_1h" in table


async def test_select_table_single_dimension_miss_continues() -> None:
    """A field with zero EQ hits is skipped; recall continues with the remaining fields."""
    client = _FakeSemanticClient()
    model = scripted_model(
        _llm_json('["cell", "downlink_volume"]'),
        _llm_json('{"family_name": "appcate_cell", "granularity": "1h"}'),
    )
    selector = _make_selector(model=model, client=client)

    def fake_search_basic(payload: dict[str, Any]) -> dict[str, Any]:
        field = _field_from_payload(payload)
        if field == "cell":
            return {"entities": []}
        return _column_search([("db", "appcate_cell_1h")])

    def fake_hybrid(tables: list[str]) -> list[dict[str, Any]]:
        return [_hybrid_payload("db", "appcate_cell_1h", ["downlink_volume", "cell"])]

    client.search_basic = fake_search_basic
    client.hybrid_table_columns = fake_hybrid

    table, _ = await selector._select_traffic_insight_table("按小区查下行流量")
    assert "appcate_cell_1h" in table


async def test_select_table_all_field_eq_miss_raises() -> None:
    """When every field EQ search returns no tables, selection fails with a clear error."""
    client = _FakeSemanticClient()
    model = scripted_model(_llm_json('["cell", "downlink_volume"]'))
    selector = _make_selector(model=model, client=client)

    def fake_search_basic(payload: dict[str, Any]) -> dict[str, Any]:
        return {"entities": []}

    client.search_basic = fake_search_basic

    with pytest.raises(DataAgentError, match="all field EQ searches returned no tables"):
        await selector._select_traffic_insight_table("按小区查下行流量")
