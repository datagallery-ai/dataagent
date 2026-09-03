from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from dataagent.agents.nl2sql.agent import _resolve_perceptor_class
from dataagent.agents.nl2sql.nodes.business_twin_perceptor import BusinessTwinPerceptorNode
from dataagent.agents.nl2sql.nodes.perceptor import PerceptorNode
from dataagent.agents.nl2sql.nodes.traffic_insight_perceptor import TrafficInsightPerceptorNode
from dataagent.agents.nl2sql.utils.sql_service import CloudCoreService, build_sql_service
from dataagent.agents.nl2sql.utils.traffic_insight_field_normalizer import (
    assert_need_fields,
    normalize_traffic_insight_fields,
)
from dataagent.agents.nl2sql.utils.traffic_insight_table_recall import (
    apply_coverage_filter,
    build_families_from_tables,
    enrich_schema_example_values_from_columns_info,
    normalize_example_values,
    select_tables_from_field_hits,
    tables_missing_from_hybrid_columns,
)
from dataagent.core.errors import DataAgentError


class _ConfigManager:
    def get(self, key: str, default: Any = None) -> Any:
        if key in {"DATABASE.db_id", "db_id"}:
            return "db"
        return default


class _FakeSemanticClient:
    def __init__(self) -> None:
        self.search_basic = lambda payload: {"entities": []}
        self.hybrid_table_columns = lambda tables: []


def _attach_semantic(node: TrafficInsightPerceptorNode, monkeypatch: pytest.MonkeyPatch) -> _FakeSemanticClient:
    client = _FakeSemanticClient()
    monkeypatch.setattr(node, "_semantic_client", client)
    return client


def _column_search(tables: list[tuple[str, str]]) -> dict[str, Any]:
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
    filters = payload["entityFilters"]
    if "criterion" in filters:
        for item in filters["criterion"]:
            if item.get("attributeName") == "column_name_en":
                return str(item["attributeValue"])
    return str(filters["attributeValue"])


@pytest.mark.parametrize(
    ("database_config", "expected"),
    [
        ({"perceptor_type": "business_twin"}, BusinessTwinPerceptorNode),
        ({"perceptor_type": "traffic_insight"}, TrafficInsightPerceptorNode),
        ({"engine": "postgres"}, PerceptorNode),
    ],
)
def test_perceptor_class_selects_configured_scenario(
    database_config: dict[str, str], expected: type[PerceptorNode]
) -> None:
    assert _resolve_perceptor_class(database_config) is expected


def test_traffic_insight_config_wires_perceptor_dialect_and_execution_service() -> None:
    config_path = Path(__file__).parents[4] / "dataagent/agents/nl2sql/traffic_insight.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    database_config = config["DATABASE"]

    assert _resolve_perceptor_class(database_config) is TrafficInsightPerceptorNode
    assert database_config["dialect"] == "postgres"
    assert database_config["perceptor_type"] == "traffic_insight"
    assert isinstance(build_sql_service(database_config["engine"], database_config["config"]), CloudCoreService)


def test_normalize_allows_metrics_only() -> None:
    need = normalize_traffic_insight_fields(["downlink_volume", "subs_count"])
    assert need["need_d"] == set()
    assert need["need_m"] == {"downlink_volume", "subs_count"}
    assert_need_fields(need["need_d"], need["need_m"])


def test_select_tables_ranks_full_hits_first() -> None:
    tables, mode = select_tables_from_field_hits(
        {
            "cell": {"db.appcate_cell_1h", "db.other_1h", "db.dim_cell"},
            "appcate": {"db.appcate_cell_1h", "db.appcate_city_1h"},
            "downlink_volume": {"db.appcate_cell_1h", "db.appcate_city_1h"},
        },
        need_d={"cell", "appcate"},
        need_m={"downlink_volume"},
        max_candidate_tables=10,
    )
    assert mode == "field_hit_count_rank"
    assert tables[0] == "db.appcate_cell_1h"
    assert "db.dim_cell" not in tables
    assert tables == ["db.appcate_cell_1h", "db.appcate_city_1h", "db.other_1h"]


def test_select_tables_partial_hits_ranked_by_score() -> None:
    tables, mode = select_tables_from_field_hits(
        {
            "cell": {"db.appcate_cell_1h", "db.cell_city_1h"},
            "appcate": {"db.appcate_city_1h"},
            "downlink_volume": {"db.appcate_cell_1h", "db.appcate_city_1h"},
            "subs_count": {"db.appcate_cell_1h"},
        },
        need_d={"cell", "appcate"},
        need_m={"downlink_volume", "subs_count"},
        max_candidate_tables=10,
    )
    assert mode == "field_hit_count_rank"
    # appcate_cell hits 3; appcate_city hits 2; cell_city hits 1
    assert tables[0] == "db.appcate_cell_1h"
    assert tables[1] == "db.appcate_city_1h"


def test_soft_coverage_keeps_partial_families() -> None:
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
    covered = apply_coverage_filter(
        families,
        columns_map,
        need_d={"cell", "appcate"},
        need_m={"downlink_volume", "subs_count"},
    )
    assert len(covered) == 1
    assert covered[0]["family_name"] == "appcate_cell"


def test_tables_missing_from_hybrid_columns() -> None:
    columns_map = {"db.a_1h": {"columns": {}}, "b_1h": {"columns": {}}}
    assert tables_missing_from_hybrid_columns(["db.a_1h", "db.b_1h", "db.c_1h"], columns_map) == ["db.c_1h"]


def test_normalize_example_values() -> None:
    assert normalize_example_values("a:b|c:d") == "a=b|c=d"
    assert normalize_example_values("") == ""
    assert normalize_example_values(None) == ""


def test_enrich_example_values_supplements_without_overwrite() -> None:
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


def test_schema_for_selected_table_hybrid_plus_values(monkeypatch: pytest.MonkeyPatch) -> None:
    node = TrafficInsightPerceptorNode(config_manager=_ConfigManager())
    _attach_semantic(node, monkeypatch)

    def fake_info(table_name: str, *, limit: int = 1000, offset: int = 0) -> dict[str, Any]:
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

    client = node.semantic_client
    client.get_table_columns_info = fake_info  # type: ignore[method-assign]
    client.hybrid_table_columns = fake_hybrid  # type: ignore[method-assign]
    schema = node._schema_for_selected_table("db.cell_1h")
    assert schema["cell_1h"]["description"] == "小区小时事实表"
    assert schema["cell_1h"]["columns"]["cell"]["description"] == "hybrid小区描述"
    assert schema["cell_1h"]["columns"]["cell"]["value_type"] == "string"
    assert schema["cell_1h"]["columns"]["cell"]["example_values"] == "A=小区A"
    assert "extra_only_info" not in schema["cell_1h"]["columns"]


def test_columns_info_single_call_no_offset_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    node = TrafficInsightPerceptorNode(config_manager=_ConfigManager())
    _attach_semantic(node, monkeypatch)
    calls: list[dict[str, Any]] = []

    def fake_info(table_name: str, *, limit: int = 1000, offset: int = 0) -> dict[str, Any]:
        calls.append({"table_name": table_name, "limit": limit, "offset": offset})
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

    client = node.semantic_client
    client.get_table_columns_info = fake_info  # type: ignore[method-assign]
    client.hybrid_table_columns = fake_hybrid  # type: ignore[method-assign]
    schema = node._schema_for_selected_table("db.wide_1h")
    assert len(calls) == 1
    assert calls[0]["offset"] == 0
    assert schema["wide_1h"]["description"] == "宽表"
    assert set(schema["wide_1h"]["columns"]) == {"tail", "col_0"}
    assert schema["wide_1h"]["columns"]["tail"]["description"] == "hybrid尾列"
    assert schema["wide_1h"]["columns"]["tail"]["example_values"] == "x=y"


def test_hybrid_multi_batch_fetches_all_and_retries_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    node = TrafficInsightPerceptorNode(config_manager=_ConfigManager())
    client = _attach_semantic(node, monkeypatch)
    node._hybrid_batch_size = 1
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
            out.append(
                {
                    "db": "db",
                    "table": bare,
                    "description": "",
                    "columns": [{"columnNameEn": "cell", "description": "", "valueType": "string"}],
                }
            )
        return out

    client.hybrid_table_columns = fake_hybrid
    result = node._hybrid_columns_for_tables(["db.cell_1h", "db.city_1h"])
    assert "db.cell_1h" in result or "cell_1h" in result
    assert "db.city_1h" in result or "city_1h" in result
    assert len(calls) >= 3  # 2 first-pass batches + 1 retry for missing city


def test_hybrid_missing_after_retry_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    node = TrafficInsightPerceptorNode(config_manager=_ConfigManager())
    client = _attach_semantic(node, monkeypatch)

    def fake_hybrid(tables: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "db": "db",
                "table": "cell_1h",
                "description": "",
                "columns": [{"columnNameEn": "cell", "description": "", "valueType": "string"}],
            }
        ]

    client.hybrid_table_columns = fake_hybrid
    with pytest.raises(DataAgentError, match="did not return all requested tables"):
        node._hybrid_columns_for_tables(["db.cell_1h", "db.missing_1h"])


def test_column_eq_pagination_streams_into_index(monkeypatch: pytest.MonkeyPatch) -> None:
    node = TrafficInsightPerceptorNode(config_manager=_ConfigManager())
    client = _attach_semantic(node, monkeypatch)
    node._column_eq_page_size = 2
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
    table_to_fields: dict[str, set[str]] = {}
    hit_tables = node._stream_field_eq_into_index("cell", table_to_fields)
    assert offsets == [0, 2]
    assert hit_tables == 3
    assert table_to_fields == {
        "db.t1_1h": {"cell"},
        "db.t2_1h": {"cell"},
        "db.t3_1h": {"cell"},
    }


def test_perceptor_soft_recall_and_resolve_with_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    node = TrafficInsightPerceptorNode(config_manager=_ConfigManager())
    client = _attach_semantic(node, monkeypatch)

    async def fake_llm(context: dict[str, Any], action: str = "") -> Any:
        if action == "filter_traffic_insight_fields_":
            return ["cell", "appcate", "downlink_volume", "subs_count"]
        if action == "filter_traffic_insight_table_family_":
            return {"family_name": "appcate_cell", "granularity": "1h"}
        raise AssertionError(f"unexpected action={action}")

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
        out = []
        for qualified in tables:
            bare = qualified.rsplit(".", 1)[-1]
            cols = ["cell", "appcate", "downlink_volume", "subs_count", "time"]
            if bare == "appcate_city_1h":
                cols = ["appcate", "city", "downlink_volume"]
            out.append(
                {
                    "db": "db",
                    "table": bare,
                    "description": "",
                    "columns": [{"columnNameEn": name, "description": "", "valueType": "string"} for name in cols],
                }
            )
        return out

    monkeypatch.setattr(node, "execute_with_llm_json", fake_llm)
    client.search_basic = fake_search_basic
    client.hybrid_table_columns = fake_hybrid

    table = asyncio.run(node._select_traffic_insight_table("各小区应用大类下行流量和用户数按小时"))
    assert table in {"appcate_cell_1h", "db.appcate_cell_1h"}


def test_perceptor_need_d_empty_continues_with_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    node = TrafficInsightPerceptorNode(config_manager=_ConfigManager())
    client = _attach_semantic(node, monkeypatch)

    async def fake_llm(context: dict[str, Any], action: str = "") -> Any:
        if action == "filter_traffic_insight_fields_":
            return ["downlink_volume"]
        if action == "filter_traffic_insight_table_family_":
            return {"family_name": "appcate_cell", "granularity": "1h"}
        raise AssertionError(action)

    def fake_search_basic(payload: dict[str, Any]) -> dict[str, Any]:
        assert _field_from_payload(payload) == "downlink_volume"
        return _column_search([("db", "appcate_cell_1h"), ("db", "dim_metric")])

    def fake_hybrid(tables: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "db": "db",
                "table": "appcate_cell_1h",
                "description": "",
                "columns": [
                    {"columnNameEn": "downlink_volume", "description": "", "valueType": "long"},
                    {"columnNameEn": "cell", "description": "", "valueType": "string"},
                ],
            }
        ]

    monkeypatch.setattr(node, "execute_with_llm_json", fake_llm)
    client.search_basic = fake_search_basic
    client.hybrid_table_columns = fake_hybrid

    table = asyncio.run(node._select_traffic_insight_table("查一下行流量"))
    assert "appcate_cell_1h" in table


def test_perceptor_single_dimension_miss_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    node = TrafficInsightPerceptorNode(config_manager=_ConfigManager())
    client = _attach_semantic(node, monkeypatch)

    async def fake_llm(context: dict[str, Any], action: str = "") -> Any:
        if action == "filter_traffic_insight_fields_":
            return ["cell", "downlink_volume"]
        if action == "filter_traffic_insight_table_family_":
            return {"family_name": "appcate_cell", "granularity": "1h"}
        raise AssertionError(action)

    def fake_search_basic(payload: dict[str, Any]) -> dict[str, Any]:
        field = _field_from_payload(payload)
        if field == "cell":
            return {"entities": []}
        return _column_search([("db", "appcate_cell_1h")])

    def fake_hybrid(tables: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "db": "db",
                "table": "appcate_cell_1h",
                "description": "",
                "columns": [
                    {"columnNameEn": "downlink_volume", "description": "", "valueType": "long"},
                    {"columnNameEn": "cell", "description": "", "valueType": "string"},
                ],
            }
        ]

    monkeypatch.setattr(node, "execute_with_llm_json", fake_llm)
    client.search_basic = fake_search_basic
    client.hybrid_table_columns = fake_hybrid

    table = asyncio.run(node._select_traffic_insight_table("按小区查下行流量"))
    assert "appcate_cell_1h" in table


def test_perceptor_all_field_eq_miss_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    node = TrafficInsightPerceptorNode(config_manager=_ConfigManager())
    client = _attach_semantic(node, monkeypatch)

    async def fake_llm(context: dict[str, Any], action: str = "") -> Any:
        return ["cell", "downlink_volume"]

    def fake_search_basic(payload: dict[str, Any]) -> dict[str, Any]:
        return {"entities": []}

    monkeypatch.setattr(node, "execute_with_llm_json", fake_llm)
    client.search_basic = fake_search_basic

    with pytest.raises(DataAgentError, match="all field EQ searches returned no tables"):
        asyncio.run(node._select_traffic_insight_table("按小区查下行流量"))
