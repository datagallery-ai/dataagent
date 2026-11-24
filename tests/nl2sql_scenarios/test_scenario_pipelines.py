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
"""End-to-end pack pipelines through ``load_scenario`` handlers.

Each test assembles a real pack and drives its ``perceptor.schema_context``
handler against a mock semantic service and a scripted model — the same
installations the host factory produces. Semantic transports assert the
``X-Semantic-Scenario`` header on every business request.
"""

import httpx
import pytest
from nl2sql.errors import DataAgentError
from nl2sql.hooks import HOOK_POINTS, PERCEPTOR_SCHEMA_CONTEXT, load_scenario

from nl2sql_scenarios.support import ScriptedModel, make_context, scripted_model, semantic_client

BT_FACT = "fact_dw1745159004_0000000000000000_metric_1h"
BT_MOS_FACT = (
    "fact_dw1745159007_0000000000008000_metric_1h"  # dw1745159007, dimension bit 15 = city
)


def bt_transport_handler(request: httpx.Request) -> httpx.Response:
    """Semantic-service mock for business_twin: two facts, derived metrics, city column."""
    assert request.headers.get("X-Semantic-Scenario") == "business_twin", dict(request.headers)
    path = request.url.path
    if path.endswith("/advanced-search/table-list"):
        return httpx.Response(
            200,
            json=[
                {f"business_twin.{BT_FACT}": {"table_description": "小区PRB指标表"}},
                {f"business_twin.{BT_MOS_FACT}": {"table_description": "MOS分析表"}},
            ],
        )
    if path.endswith("/advanced-search/table-columns-info"):
        table = request.url.params.get("tableName", "")
        if table.endswith("derived_metrics"):
            return httpx.Response(
                200,
                json={
                    "business_twin.derived_metrics.cell_prb_ul_usage_rate": {
                        "column_short_description": "上行PRB利用率",
                        "column_value_profile": "cell_prb_ul_usage / cell_prb_ul_total",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                f"{table}.avg_qoe": {
                    "column_short_description": "平均QoE",
                    "value_type": "DOUBLE",
                    "value_description": "4.2",
                },
                f"{table}.city": {
                    "column_short_description": "地市",
                    "value_type": "TEXT",
                    "value_description": "320200:无锡市",
                },
            },
        )
    if path.endswith("/advanced-search/joinable-tables"):
        return httpx.Response(200, json=[])
    raise AssertionError(f"unexpected request {request.method} {path}")


def ti_transport_handler(request: httpx.Request) -> httpx.Response:
    """Semantic-service mock for traffic_insight: one traffic fact with two columns."""
    assert request.headers.get("X-Semantic-Scenario") == "traffic_insight", dict(request.headers)
    path = request.url.path
    if path.endswith("/search/basic"):
        return httpx.Response(
            200,
            json={
                "entities": [
                    {
                        "attributes": {
                            "db_name_en": "yunhe",
                            "table_name_en": "dwd_app_traffic_1h",
                            "column_name_en": "app",
                        }
                    },
                ]
            },
        )
    if path.endswith("/hybrid/table-columns"):
        return httpx.Response(
            200,
            json=[
                {
                    "table": "dwd_app_traffic_1h",
                    "db": "yunhe",
                    "description": "应用流量表",
                    "columns": [
                        {"columnNameEn": "app", "description": "应用", "valueType": "TEXT"},
                        {
                            "columnNameEn": "uplink_volume",
                            "description": "上行流量",
                            "valueType": "BIGINT",
                        },
                    ],
                }
            ],
        )
    if path.endswith("/advanced-search/table-columns-info"):
        return httpx.Response(
            200,
            json={
                "yunhe.dwd_app_traffic_1h.app": {
                    "column_short_description": "应用",
                    "value_description": "WeChat",
                },
            },
        )
    raise AssertionError(f"unexpected request {request.method} {path}")


@pytest.mark.parametrize("scenario", ["udn.business_twin", "udn.traffic_insight"])
async def test_pack_assembles_with_every_hook_point(scenario: str) -> None:
    handlers = load_scenario(make_context(scenario, ScriptedModel(responses=[]), None))
    assert set(handlers) == HOOK_POINTS
    assert all(callable(handler) for handler in handlers.values())


async def test_business_twin_pinned_table_selection_skips_the_model() -> None:
    """A question naming a table selects it directly — zero LLM calls."""
    model = ScriptedModel(responses=[])
    with semantic_client(bt_transport_handler, scenario="business_twin") as client:
        handlers = load_scenario(make_context("udn.business_twin", model, client))
        context = await handlers[PERCEPTOR_SCHEMA_CONTEXT](
            f"查一下 {BT_FACT} 中无锡市的小区上行PRB利用率"
        )

    assert list(context.schema) == [BT_FACT]
    columns = context.schema[BT_FACT]["columns"]
    assert columns["city"]["example_values"] == "320200=无锡市"
    assert context.joins == []
    assert "CREATE TABLE" in context.schema_str
    assert context.extra_rules.startswith("# SQL Generation Rules")
    assert "现在为" in context.extra_rules
    assert "请基于当前日期解析用户问题中的相对时间" in context.extra_rules
    assert "## 行政区划匹配" in context.extra_rules
    assert "## 时间粒度" not in context.extra_rules  # pinned table claims no granularity
    assert model.requests == []


async def test_business_twin_llm_selection_chain() -> None:
    """Column extraction → business-ID routing → family selection → resolved table."""
    model = scripted_model(
        '```json\n["avg_qoe", "city"]\n```',  # LLM 1: column extraction
        '```json\n{"family_name": "fact_dw1745159007_0000000000008000", '
        '"granularity": "1h", "explicit_granularity": true}\n```',  # LLM 2: family + granularity
    )
    with semantic_client(bt_transport_handler, scenario="business_twin") as client:
        handlers = load_scenario(make_context("udn.business_twin", model, client))
        context = await handlers[PERCEPTOR_SCHEMA_CONTEXT]("各地市的平均QoE")

    assert list(context.schema) == [BT_MOS_FACT]
    assert "CREATE TABLE" in context.schema_str and "avg_qoe" in context.schema_str
    assert context.extra_rules.startswith("# SQL Generation Rules")
    assert "## 时间粒度" in context.extra_rules  # explicit granularity was selected
    assert len(model.requests) == 2


async def test_traffic_insight_pipeline() -> None:
    """Field EQ recall → family selection → hybrid schema, with the granularity rule."""
    model = scripted_model(
        '```json\n["app", "uplink_volume", "time"]\n```',  # LLM 1: "time" is a pseudo field
        '```json\n{"family_name": "dwd_app_traffic", "granularity": "1h", '
        '"explicit_granularity": true}\n```',  # LLM 2: family + granularity
    )
    with semantic_client(ti_transport_handler, scenario="traffic_insight") as client:
        handlers = load_scenario(make_context("udn.traffic_insight", model, client, db_id="yunhe"))
        context = await handlers[PERCEPTOR_SCHEMA_CONTEXT]("各应用的上行流量，按小时统计")

    assert list(context.schema) == ["dwd_app_traffic_1h"]
    columns = context.schema["dwd_app_traffic_1h"]["columns"]
    assert set(columns) == {"app", "uplink_volume"}
    assert columns["app"]["example_values"] == "WeChat"
    assert context.joins == []
    assert "CREATE TABLE" in context.schema_str and "dwd_app_traffic_1h" in context.schema_str
    assert context.extra_rules.startswith("# SQL Generation Rules")
    assert "现在为" in context.extra_rules
    assert (
        "请基于当前日期解析用户问题中的相对时间" not in context.extra_rules
    )  # ti has no such hint
    assert "## 时间粒度" in context.extra_rules
    assert len(model.requests) == 2


async def test_traffic_insight_without_client_reports_config_error() -> None:
    """A missing semantic client surfaces as a config error, not a crash."""
    model = scripted_model('```json\n["app"]\n```')
    handlers = load_scenario(make_context("udn.traffic_insight", model, None, db_id="yunhe"))
    with pytest.raises(DataAgentError) as excinfo:
        await handlers[PERCEPTOR_SCHEMA_CONTEXT]("各应用的上行流量")
    assert excinfo.value.source == "config"
    assert "semantic 服务未配置" in str(excinfo.value)
