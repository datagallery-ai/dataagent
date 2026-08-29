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
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from dataagent.agents.nl2sql.nodes.generator import GeneratorNode
from dataagent.agents.nl2sql.security import check_sql
from dataagent.agents.nl2sql.utils.nl2sql_utils import load_dimension_mappings, schema_to_ddl, selected_dimensions
from dataagent.agents.nl2sql.workflow.state import get_default_state
from dataagent.config.config_manager import ConfigManager
from dataagent.core.managers.llm_manager import llm_manager


def _config_manager(scenario: str) -> ConfigManager:
    manager = ConfigManager()
    manager.settings = {
        "DATABASE": {
            "dialect": "postgres",
            "perceptor_type": scenario,
        }
    }
    return manager


def _node(scenario: str = "business_twin") -> GeneratorNode:
    return GeneratorNode(
        config_manager=_config_manager(scenario),
        num_samples=1,
        num_workers=1,
        strategies=["prompt"],
    )


def _state():
    schema = {
        "fact_metric": {
            "description": "fact",
            "columns": {
                "time": {"value_type": "bigint"},
                "county": {"value_type": "integer"},
                "uplink_traffic": {"value_type": "numeric"},
            },
        }
    }
    return get_default_state(
        "query traffic by county",
        schema=schema,
        schema_str=schema_to_ddl(schema),
        sql_rules="Keep the requested time range.",
    )


@pytest.mark.parametrize(
    ("scenario", "dimension", "table"),
    [
        ("business_twin", "county", "dim_exp_county"),
        ("traffic_insight", "app", "dim_app"),
    ],
)
def test_loads_packaged_scenario_dimension_mapping(scenario: str, dimension: str, table: str) -> None:
    mappings = load_dimension_mappings(scenario)

    assert mappings[dimension]["dimension_table"] == table


def test_detects_only_standalone_final_select_dimensions() -> None:
    mappings = load_dimension_mappings("business_twin")

    assert selected_dimensions("SELECT county, time FROM fact_metric", mappings, "postgres") == ["county"]
    assert selected_dimensions("SELECT f.county AS area FROM fact_metric f", mappings, "postgres") == ["county"]
    assert selected_dimensions(
        "WITH x AS (SELECT county FROM fact_metric) SELECT x.county FROM x",
        mappings,
        "postgres",
    ) == ["county"]
    assert selected_dimensions(
        "SELECT q.county FROM (SELECT county FROM fact_metric) q",
        mappings,
        "postgres",
    ) == ["county"]
    assert (
        selected_dimensions(
            "WITH x AS (SELECT county FROM fact_metric) SELECT COUNT(*) FROM x",
            mappings,
            "postgres",
        )
        == []
    )
    assert (
        selected_dimensions(
            "SELECT SUM(CASE WHEN county = 1 THEN uplink_traffic ELSE 0 END) FROM fact_metric",
            mappings,
            "postgres",
        )
        == []
    )


@pytest.mark.asyncio
async def test_rewrites_dimension_and_augments_downstream_context() -> None:
    original = (
        "SELECT time, county, SUM(uplink_traffic) AS uplink_traffic FROM fact_metric "
        "WHERE county = 10 GROUP BY time, county ORDER BY county"
    )
    rewritten = (
        "SELECT f.time, d.county_value, SUM(f.uplink_traffic) AS uplink_traffic FROM fact_metric f "
        "LEFT JOIN dim_exp_county d ON f.county = d.county_key "
        "WHERE f.county = 10 GROUP BY f.time, d.county_value ORDER BY d.county_value"
    )
    llm = SimpleNamespace(
        ainvoke=AsyncMock(
            side_effect=[
                SimpleNamespace(content=f"```sql\n{original}\n```"),
                SimpleNamespace(content=f"```sql\n{rewritten}\n```"),
            ]
        )
    )

    with patch.object(llm_manager, "get_default_llm", return_value=llm):
        result = await _node()._aprocess(_state())

    assert llm.ainvoke.await_count == 2
    assert result["sql"] == rewritten
    assert result["generation_results"][0].need_ref is False
    assert result["schema"]["dim_exp_county"]["columns"].keys() >= {"county_key", "county_value"}
    assert ("fact_metric.county", "dim_exp_county.county_key") in result["joins"]
    assert "CREATE TABLE `dim_exp_county`" in result["schema_str"]
    assert "WHERE/HAVING predicates remain on the fact key" in result["sql_rules"]
    assert "dim_exp_county" in result["generation_results"][0].prompt
    assert check_sql(rewritten, dialect="postgres", schema=result["schema"]).violations == []
    assert check_sql(rewritten, dialect="postgres", schema=_state()["schema"]).violations[0].rule_id == "SCHEMA-001"

    rewrite_prompts = llm.ainvoke.await_args_list[1].args[0]
    assert '"county":{"dimension_table":"dim_exp_county"' in rewrite_prompts[1]["content"]
    assert "Keep existing `WHERE` and `HAVING` predicates on the fact key" in rewrite_prompts[0]["content"]


@pytest.mark.asyncio
async def test_skips_rewrite_when_final_select_has_no_dimension() -> None:
    original = "SELECT SUM(uplink_traffic) AS uplink_traffic FROM fact_metric WHERE time > 0"
    llm = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=f"```sql\n{original}\n```")))

    with patch.object(llm_manager, "get_default_llm", return_value=llm):
        result = await _node()._aprocess(_state())

    assert llm.ainvoke.await_count == 1
    assert result["sql"] == original
    assert "dim_exp_county" not in result["schema"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rewrite_outcome",
    [
        RuntimeError("rewrite unavailable"),
        SimpleNamespace(content="```sql\nSELECT (\n```"),
    ],
    ids=["llm-error", "invalid-sql"],
)
async def test_keeps_original_and_requests_reflection_when_rewrite_fails(rewrite_outcome) -> None:
    original = "SELECT county, SUM(uplink_traffic) FROM fact_metric WHERE time > 0 GROUP BY county"
    llm = SimpleNamespace(
        ainvoke=AsyncMock(
            side_effect=[
                SimpleNamespace(content=f"```sql\n{original}\n```"),
                rewrite_outcome,
            ]
        )
    )

    with patch.object(llm_manager, "get_default_llm", return_value=llm):
        result = await _node()._aprocess(_state())

    candidate = result["generation_results"][0]
    assert candidate.sql == original
    assert candidate.need_ref is True
    assert "dim_exp_county" in result["schema"]
    assert "dim_exp_county" in candidate.prompt
