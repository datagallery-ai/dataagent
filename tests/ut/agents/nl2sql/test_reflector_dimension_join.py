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

from dataagent.agents.nl2sql.nodes.reflector import ReflectorNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import schema_to_ddl
from dataagent.agents.nl2sql.workflow.state import Result, get_default_state
from dataagent.config.config_manager import ConfigManager
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.prompt_manager import PromptTemplate

BARE_SQL = (
    "SELECT time, county, SUM(uplink_traffic) AS uplink_traffic FROM fact_metric "
    "WHERE county = 10 GROUP BY time, county"
)
REWRITTEN_SQL = (
    "SELECT f.time, d.county_value, SUM(f.uplink_traffic) AS uplink_traffic FROM fact_metric f "
    "INNER JOIN dim_exp_county d ON f.county = d.county_key "
    "WHERE f.county = 10 GROUP BY f.time, d.county_value"
)


def _config_manager(scenario: str = "business_twin") -> ConfigManager:
    manager = ConfigManager()
    manager.settings = {
        "DATABASE": {
            "dialect": "postgres",
            "perceptor_type": scenario,
        }
    }
    return manager


def _node() -> ReflectorNode:
    return ReflectorNode(config_manager=_config_manager(), threshold=0.9)


def _state() -> dict:
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
    state = get_default_state(
        "query traffic by county",
        schema=schema,
        schema_str=schema_to_ddl(schema),
        sql_rules="Keep the requested time range.",
        ref_retries=1,
    )
    state["validation_results"] = [
        Result(
            id=0,
            sql="SELECT 1",
            prompt="original generator prompt",
            score=0.1,
            need_ref=True,
            security_checked=True,
        )
    ]
    return state


@pytest.mark.asyncio
async def test_reflector_dimension_join_loads_generator_prompts_not_reflector() -> None:
    node = _node()
    node.execute_with_llm_json = AsyncMock(return_value=[{"id": 0, "sql": BARE_SQL}])
    loaded: list[str] = []
    real_loader = PromptTemplate.from_package_relative

    def tracking_loader(path: str):
        loaded.append(path)
        return real_loader(path)

    llm = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=f"```sql\n{REWRITTEN_SQL}\n```")))

    with (
        patch.object(PromptTemplate, "from_package_relative", side_effect=tracking_loader),
        patch.object(llm_manager, "get_default_llm", return_value=llm),
    ):
        result = await node._aprocess(_state())

    assert any("generator/dimension_join_" in path for path in loaded)
    assert not any("reflector/dimension_join_" in path for path in loaded)
    assert "INNER JOIN dim_exp_county" in result["generation_results"][0].sql
    assert llm.ainvoke.await_count == 1
    assert (
        "Keep existing `WHERE` and `HAVING` predicates on the fact key" in llm.ainvoke.await_args.args[0][0]["content"]
    )


@pytest.mark.asyncio
async def test_reflector_keeps_reflected_sql_when_dimension_join_fails() -> None:
    node = _node()
    node.execute_with_llm_json = AsyncMock(return_value=[{"id": 0, "sql": BARE_SQL}])
    llm = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("rewrite unavailable")))

    with patch.object(llm_manager, "get_default_llm", return_value=llm):
        result = await node._aprocess(_state())

    candidate = result["generation_results"][0]
    assert candidate.sql == BARE_SQL
    assert candidate.need_ref is True
