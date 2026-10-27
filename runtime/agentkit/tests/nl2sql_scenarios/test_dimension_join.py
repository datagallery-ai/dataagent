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
"""Dimension-join SQL rewriting through the shared UDN rewriter closure.

Merged port of the old branch's ``test_generator_dimension_join.py`` and
``test_reflector_dimension_join.py``. The old architecture kept the
dimension-join machinery in ``nl2sql_utils`` and invoked it separately from
the generator and reflector nodes; the scenario-pack architecture builds one
``dimension_join_rewriter`` closure and installs it at BOTH ``post_sql`` hook
points, so the former per-node tests drive the closure directly with
``(results, state)``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nl2sql.security import check_sql
from nl2sql.state import Result, get_default_state
from nl2sql.utils.nl2sql_utils import schema_to_ddl
from scenarios.udn.common.dimension_join import (
    dimension_join_rewriter,
    load_dimensions,
    selected_dimensions,
)

from nl2sql_scenarios.support import scripted_model

PACK_ROOT = (
    Path(__file__).resolve().parents[2] / "builtin-plugins" / "nl2sql" / "scenarios" / "udn"
)
COMMON_PROMPTS = PACK_ROOT / "common" / "prompts"

# The reflector-flavored pair from the old tests: a bare projection of the
# mapped ``county`` column, and its dimension-lookup rewrite.
BARE_SQL = (
    "SELECT time, county, SUM(uplink_traffic) AS uplink_traffic FROM fact_metric "
    "WHERE county = 10 GROUP BY time, county"
)
REWRITTEN_SQL = (
    "SELECT f.time, d.county_value, SUM(f.uplink_traffic) AS uplink_traffic FROM fact_metric f "
    "INNER JOIN dim_exp_county d ON f.county = d.county_key "
    "WHERE f.county = 10 GROUP BY f.time, d.county_value"
)
# The generator-flavored pair adds an ORDER BY the rewrite must move to the value column.
GENERATOR_SQL = f"{BARE_SQL} ORDER BY county"
GENERATOR_REWRITTEN = f"{REWRITTEN_SQL} ORDER BY d.county_value"
# A second-round (reflector) candidate projecting an additional mapped dimension.
CITY_BARE_SQL = (
    "SELECT time, city, county, SUM(uplink_traffic) AS uplink_traffic FROM fact_metric "
    "WHERE time > 0 GROUP BY time, city, county"
)
CITY_REWRITTEN_SQL = (
    "SELECT f.time, c.city_value, n.county_value, SUM(f.uplink_traffic) AS uplink_traffic "
    "FROM fact_metric f "
    "INNER JOIN dim_exp_city c ON f.city = c.city_key "
    "INNER JOIN dim_exp_county n ON f.county = n.county_key "
    "WHERE f.time > 0 GROUP BY f.time, c.city_value, n.county_value"
)


def _mappings(scenario: str) -> dict[str, dict[str, str]]:
    """Load one pack's packaged dimension mappings."""
    return load_dimensions(PACK_ROOT / scenario / "dimensions.json", scenario)


def _fact_state() -> dict:
    """A minimal run state over the ``fact_metric`` fact table."""
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
    """Both packs ship their fact-to-dimension mappings next to their entry point."""
    mappings = _mappings(scenario)

    assert mappings[dimension]["dimension_table"] == table
    if scenario == "business_twin":
        # ``ne_name`` is plain fact text, never a dimension lookup.
        assert "ne_name" not in mappings


def test_detects_only_standalone_final_select_dimensions() -> None:
    """Only standalone mapped columns of the final SELECT projection count."""
    bt = _mappings("business_twin")
    ti = _mappings("traffic_insight")

    assert selected_dimensions("SELECT county, time FROM fact_metric", bt, "postgres") == ["county"]
    assert selected_dimensions("SELECT f.county AS area FROM fact_metric f", bt, "postgres") == ["county"]
    assert selected_dimensions("SELECT `county` FROM fact_metric", bt, "mysql") == ["county"]
    assert selected_dimensions(
        "WITH x AS (SELECT county FROM fact_metric) SELECT x.county FROM x",
        bt,
        "postgres",
    ) == ["county"]
    assert selected_dimensions(
        "SELECT q.county FROM (SELECT county FROM fact_metric) q",
        bt,
        "postgres",
    ) == ["county"]
    assert (
        selected_dimensions(
            "WITH x AS (SELECT county FROM fact_metric) SELECT COUNT(*) FROM x",
            bt,
            "postgres",
        )
        == []
    )
    assert (
        selected_dimensions(
            "SELECT SUM(CASE WHEN county = 1 THEN uplink_traffic ELSE 0 END) FROM fact_metric",
            bt,
            "postgres",
        )
        == []
    )
    assert selected_dimensions("SELECT app FROM dwd_app_traffic_1h", ti, "postgres") == ["app"]


async def test_rewrites_dimension_and_augments_downstream_context() -> None:
    """Success path: the projected dimension becomes a value-lookup join.

    The rewritten SQL must pass the security check against the augmented
    schema and fail with SCHEMA-001 against the bare fact schema — the
    injected dimension context is what lets the validator re-check the rewrite.
    """
    model = scripted_model(f"```sql\n{GENERATOR_REWRITTEN}\n```")
    rewrite = dimension_join_rewriter(_mappings("business_twin"), model, "postgres")
    results = [Result(id=0, sql=GENERATOR_SQL, prompt="generator prompt")]
    state = _fact_state()

    out = await rewrite(results, state)

    assert out is results
    assert len(model.requests) == 1
    assert results[0].sql == GENERATOR_REWRITTEN
    assert results[0].need_ref is False
    assert state["schema"]["dim_exp_county"]["columns"].keys() >= {"county_key", "county_value"}
    assert ("fact_metric.county", "dim_exp_county.county_key") in state["joins"]
    assert "## Dimension Lookup Schema" in state["schema_str"]
    assert "CREATE TABLE `dim_exp_county`" in state["schema_str"]
    assert "## Required Dimension Value Mappings" in state["sql_rules"]
    assert "WHERE/HAVING predicates remain on the fact key" in state["sql_rules"]
    assert results[0].prompt.startswith("generator prompt")
    assert "## Dimension Lookup Schema" in results[0].prompt
    assert "dim_exp_county" in results[0].prompt
    assert check_sql(GENERATOR_REWRITTEN, dialect="postgres", schema=state["schema"]).violations == []
    assert (
        check_sql(GENERATOR_REWRITTEN, dialect="postgres", schema=_fact_state()["schema"])
        .violations[0]
        .rule_id
        == "SCHEMA-001"
    )

    system_message, user_message = model.requests[0]
    assert "Keep existing `WHERE` and `HAVING` predicates on the fact key" in system_message.content
    assert '"county":{"dimension_table":"dim_exp_county"' in user_message.content
    assert GENERATOR_SQL in user_message.content


async def test_skips_rewrite_when_final_select_has_no_dimension() -> None:
    """No mapped projection: the SQL, state, and prompt stay untouched, no model call."""
    model = scripted_model()
    rewrite = dimension_join_rewriter(_mappings("business_twin"), model, "postgres")
    original = "SELECT SUM(uplink_traffic) AS uplink_traffic FROM fact_metric WHERE time > 0"
    results = [Result(id=0, sql=original, prompt="generator prompt")]
    state = _fact_state()

    out = await rewrite(results, state)

    assert out is results
    assert results[0].sql == original
    assert results[0].need_ref is False
    assert results[0].prompt == "generator prompt"
    assert "dim_exp_county" not in state["schema"]
    assert state["joins"] == []
    assert state["sql_rules"] == "Keep the requested time range."
    assert model.requests == []


@pytest.mark.parametrize(
    "response",
    [None, "```sql\nSELECT (\n```", "抱歉，我无法改写这条SQL"],
    ids=["llm-error", "invalid-sql", "model-garbage"],
)
async def test_keeps_original_and_requests_reflection_when_rewrite_fails(response: str | None) -> None:
    """A failed rewrite keeps the original SQL and flags ``need_ref`` for another round.

    Merges the old generator parametrized failure test with the old reflector
    failure test: both drove the same per-candidate catch, which is now the one
    shared closure. The dimension context is still injected so the validator
    can re-check the untouched SQL.
    """
    model = scripted_model() if response is None else scripted_model(response)
    rewrite = dimension_join_rewriter(_mappings("business_twin"), model, "postgres")
    original = "SELECT county, SUM(uplink_traffic) FROM fact_metric WHERE time > 0 GROUP BY county"
    results = [Result(id=0, sql=original, prompt="generator prompt")]
    state = _fact_state()

    out = await rewrite(results, state)

    assert out is results
    assert results[0].sql == original
    assert results[0].need_ref is True
    assert "dim_exp_county" in state["schema"]
    assert "dim_exp_county" in results[0].prompt


async def test_dimension_context_appends_only_newly_discovered_dimensions() -> None:
    """A second rewrite round appends context only for dimensions it newly discovers.

    Port of the old ``_add_dimension_context`` unit test through the public
    closure: the generator round injects ``county``; a later (reflector) round
    projecting ``city`` and ``county`` must append only ``city``'s DDL and
    rule, leaving the shared state deduplicated.
    """
    model = scripted_model(
        f"```sql\n{REWRITTEN_SQL}\n```",
        f"```sql\n{CITY_REWRITTEN_SQL}\n```",
    )
    rewrite = dimension_join_rewriter(_mappings("business_twin"), model, "postgres")
    state = _fact_state()

    # Generator round: the candidate projects ``county`` only.
    generated = [Result(id=0, sql=BARE_SQL, prompt="generator prompt")]
    await rewrite(generated, state)

    # Reflector round with the same closure: the repaired SQL projects ``city`` too.
    repaired = [Result(id=0, sql=CITY_BARE_SQL, prompt="repaired prompt")]
    await rewrite(repaired, state)

    assert "dim_exp_county" in generated[0].prompt
    assert "When `county` is projected" in generated[0].prompt
    assert "dim_exp_city" in repaired[0].prompt
    assert "When `city` is projected" in repaired[0].prompt
    assert "When `county` is projected" not in repaired[0].prompt
    assert "dim_exp_county" not in repaired[0].prompt
    assert state["schema_str"].count("CREATE TABLE `dim_exp_county`") == 1
    assert state["schema_str"].count("CREATE TABLE `dim_exp_city`") == 1
    assert state["sql_rules"].count("When `county` is projected") == 1
    assert state["sql_rules"].count("When `city` is projected") == 1
    assert state["joins"] == [
        ("fact_metric.county", "dim_exp_county.county_key"),
        ("fact_metric.city", "dim_exp_city.city_key"),
    ]


async def test_reflector_round_uses_the_shared_common_prompts() -> None:
    """The closure drives both hook points with the shared ``common/prompts`` pair.

    The old architecture kept per-node prompt directories and the reflector had
    to be pinned to the generator's pair; the merged architecture backs the one
    shared closure with ``scenarios/udn/common/prompts``, so the reflector round
    must send exactly that system/user pair.
    """
    model = scripted_model(f"```sql\n{REWRITTEN_SQL}\n```")
    rewrite = dimension_join_rewriter(_mappings("business_twin"), model, "postgres")
    results = [Result(id=0, sql=BARE_SQL, prompt="original generator prompt")]
    state = _fact_state()

    await rewrite(results, state)

    assert results[0].sql == REWRITTEN_SQL
    assert "INNER JOIN dim_exp_county" in results[0].sql
    assert len(model.requests) == 1
    system_message, user_message = model.requests[0]
    assert system_message.content == (COMMON_PROMPTS / "dimension_join_system.md").read_text(
        encoding="utf-8"
    )
    assert "Keep existing `WHERE` and `HAVING` predicates on the fact key" in system_message.content
    assert '"county":{"dimension_table":"dim_exp_county"' in user_message.content
    assert "postgres" in user_message.content
    assert BARE_SQL in user_message.content
