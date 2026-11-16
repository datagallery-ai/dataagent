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

Merged port of the old branch's ``test_generator_dimension_join.py``,
``test_reflector_dimension_join.py``, and ``test_dimension_join_rewrite.py``.
The old architecture kept the dimension-join machinery in ``nl2sql_utils`` and
invoked it separately from the generator and reflector nodes; the scenario-pack
architecture builds one ``dimension_join_rewriter`` closure and installs it at
BOTH ``post_sql`` hook points, so the former per-node tests drive the closure
directly with ``(results, state)``.

Since br_release_1230 the rewrite is deterministic first: a SQLGlot rewriter
swaps the projected (or GROUP BY-only) fact dimensions for their lookup-table
values and adds the INNER JOIN, verified by atomic postconditions; the LLM
rewrite only runs for shapes the deterministic path declines (ordinal GROUP BY,
subquery FROM) or when the rewrite fails postconditions. The unit tests at the
bottom pin the deterministic rewriter helper-by-helper.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot
from nl2sql.security import check_sql
from nl2sql.state import Result, get_default_state
from nl2sql.utils.nl2sql_utils import schema_to_ddl
from scenarios.udn.common.dimension_join import (
    _dimension_postconditions,
    _grouped_dimension_columns,
    _projected_dimension_columns,
    _rewrite_dimensions_with_sqlglot,
    _source_tables,
    dimension_join_rewriter,
    load_dimensions,
    selected_dimensions,
)
from sqlglot import exp

from nl2sql_scenarios.support import scripted_model

PACK_ROOT = (
    Path(__file__).resolve().parents[2] / "builtin-plugins" / "nl2sql" / "scenarios" / "udn"
)
COMMON_PROMPTS = PACK_ROOT / "common" / "prompts"

# A bare projection of the mapped ``county`` column plus an ORDER BY that the
# rewrite must move to the lookup value column.
GENERATOR_SQL = (
    "SELECT time, county, SUM(uplink_traffic) AS uplink_traffic FROM fact_metric "
    "WHERE county = 10 GROUP BY time, county ORDER BY county"
)
# What the deterministic SQLGlot rewriter renders for GENERATOR_SQL.
MECHANICAL_REWRITTEN = (
    "SELECT time, dim_exp_county.county_value AS county, SUM(uplink_traffic) AS uplink_traffic "
    "FROM fact_metric INNER JOIN dim_exp_county ON fact_metric.county = dim_exp_county.county_key "
    "WHERE fact_metric.county = 10 GROUP BY time, dim_exp_county.county_value "
    "ORDER BY dim_exp_county.county_value"
)
# The reflector-flavored pair from the old tests: ordinal GROUP BY declines the
# deterministic path, so the scripted LLM rewrite (with its own aliases) applies.
ORDINAL_BARE_SQL = (
    "SELECT time, county, SUM(uplink_traffic) AS uplink_traffic FROM fact_metric "
    "WHERE county = 10 GROUP BY 1, county"
)
REWRITTEN_SQL = (
    "SELECT f.time, d.county_value, SUM(f.uplink_traffic) AS uplink_traffic FROM fact_metric f "
    "INNER JOIN dim_exp_county d ON f.county = d.county_key "
    "WHERE f.county = 10 GROUP BY f.time, d.county_value"
)
# A second-round (reflector) candidate projecting an additional mapped dimension.
BARE_SQL = (
    "SELECT time, county, SUM(uplink_traffic) AS uplink_traffic FROM fact_metric "
    "WHERE county = 10 GROUP BY time, county"
)
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
# The real lost-dimension shape: sub_app_id is grouped and filtered but never
# projected, and the lookup key shares the fact column's name.
LOST_DIMENSION_SQL = (
    "SELECT ne_name, SUM(kpi_value) AS kpi_value FROM fact_5g_user_experience "
    "WHERE time >= 100 AND time < 200 AND sub_app_id IN ('app1', 'app2') "
    "GROUP BY ne_name, sub_app_id"
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


async def test_rewrites_dimension_deterministically_and_augments_downstream_context() -> None:
    """Success path: the projected dimension becomes a value-lookup join, with no LLM call.

    The deterministic rewriter produces the rewrite; the scripted model is
    exhausted, so any fallback request would raise. The rewritten SQL must pass
    the security check against the augmented schema and fail with SCHEMA-001
    against the bare fact schema — the injected dimension context is what lets
    the validator re-check the rewrite.
    """
    model = scripted_model()
    rewrite = dimension_join_rewriter(_mappings("business_twin"), model, "postgres")
    results = [Result(id=0, sql=GENERATOR_SQL, prompt="generator prompt")]
    state = _fact_state()

    out = await rewrite(results, state)

    assert out is results
    assert model.requests == []
    assert results[0].sql == MECHANICAL_REWRITTEN
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
    assert check_sql(MECHANICAL_REWRITTEN, dialect="postgres", schema=state["schema"]).violations == []
    assert (
        check_sql(MECHANICAL_REWRITTEN, dialect="postgres", schema=_fact_state()["schema"])
        .violations[0]
        .rule_id
        == "SCHEMA-001"
    )


async def test_backfills_dimension_grouped_but_not_projected() -> None:
    """A dimension referenced only in GROUP BY is backfilled into the projection."""
    model = scripted_model()
    rewrite = dimension_join_rewriter(_mappings("business_twin"), model, "postgres")
    original = (
        "SELECT time, SUM(uplink_traffic) AS uplink_traffic FROM fact_metric "
        "WHERE county = 10 GROUP BY time, county"
    )
    expected = (
        "SELECT time, dim_exp_county.county_value AS county, SUM(uplink_traffic) AS uplink_traffic "
        "FROM fact_metric INNER JOIN dim_exp_county ON fact_metric.county = dim_exp_county.county_key "
        "WHERE fact_metric.county = 10 GROUP BY time, dim_exp_county.county_value"
    )
    results = [Result(id=0, sql=original, prompt="generator prompt")]
    state = _fact_state()

    await rewrite(results, state)

    assert model.requests == []
    assert results[0].sql == expected
    assert results[0].need_ref is False
    assert "When `county` is projected" in state["sql_rules"]


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


async def test_skips_rewrite_when_dimension_already_joined() -> None:
    """A candidate that already projects the lookup value has nothing left to do."""
    model = scripted_model()
    rewrite = dimension_join_rewriter(_mappings("business_twin"), model, "postgres")
    original = (
        "SELECT time, dim_exp_county.county_value AS county, SUM(uplink_traffic) AS uplink_traffic "
        "FROM fact_metric INNER JOIN dim_exp_county ON fact_metric.county = dim_exp_county.county_key "
        "WHERE fact_metric.county = 10 GROUP BY time, dim_exp_county.county_value ORDER BY county"
    )
    results = [Result(id=0, sql=original, prompt="generator prompt")]
    state = _fact_state()

    out = await rewrite(results, state)

    assert out is results
    assert model.requests == []
    assert results[0].sql == original
    assert results[0].need_ref is False
    assert results[0].prompt == "generator prompt"
    assert "dim_exp_county" not in state["schema"]


@pytest.mark.parametrize(
    "response",
    [None, "```sql\nSELECT (\n```", "抱歉，我无法改写这条SQL"],
    ids=["llm-error", "invalid-sql", "model-garbage"],
)
async def test_keeps_original_and_requests_reflection_when_rewrite_fails(response: str | None) -> None:
    """A failed rewrite keeps the original SQL and flags ``need_ref`` for another round.

    Ordinal GROUP BY declines the deterministic path, so the LLM rewrite runs
    and fails. The dimension context is still injected so the validator can
    re-check the untouched SQL.
    """
    model = scripted_model() if response is None else scripted_model(response)
    rewrite = dimension_join_rewriter(_mappings("business_twin"), model, "postgres")
    original = "SELECT county, SUM(uplink_traffic) FROM fact_metric WHERE time > 0 GROUP BY 1"
    results = [Result(id=0, sql=original, prompt="generator prompt")]
    state = _fact_state()

    out = await rewrite(results, state)

    assert out is results
    assert results[0].sql == original
    assert results[0].need_ref is True
    assert len(model.requests) == 1
    assert "dim_exp_county" in state["schema"]
    assert "dim_exp_county" in results[0].prompt


async def test_reverts_half_applied_llm_rewrite() -> None:
    """A rewrite that joins and regroups but leaves the bare fact column projected is rejected."""
    model = scripted_model(
        "```sql\n"
        "SELECT time, county, SUM(uplink_traffic) AS uplink_traffic FROM fact_metric "
        "INNER JOIN dim_exp_county ON fact_metric.county = dim_exp_county.county_key "
        "WHERE fact_metric.county = 10 GROUP BY time, dim_exp_county.county_value"
        "\n```"
    )
    rewrite = dimension_join_rewriter(_mappings("business_twin"), model, "postgres")
    results = [Result(id=0, sql=ORDINAL_BARE_SQL, prompt="generator prompt")]
    state = _fact_state()

    await rewrite(results, state)

    assert len(model.requests) == 1
    assert results[0].sql == ORDINAL_BARE_SQL
    assert results[0].need_ref is True
    assert "dim_exp_county" in state["schema"]
    assert "dim_exp_county" in results[0].prompt


async def test_falls_back_to_llm_for_subquery_from() -> None:
    """A subquery FROM has no plain fact table, so the LLM rewrite runs."""
    model = scripted_model(
        "```sql\n"
        "SELECT d.county_value, SUM(t.uplink_traffic) FROM "
        "(SELECT county, uplink_traffic FROM fact_metric WHERE time > 0) t "
        "INNER JOIN dim_exp_county d ON t.county = d.county_key GROUP BY d.county_value"
        "\n```"
    )
    rewrite = dimension_join_rewriter(_mappings("business_twin"), model, "postgres")
    original = (
        "SELECT county, SUM(uplink_traffic) FROM "
        "(SELECT county, uplink_traffic FROM fact_metric WHERE time > 0) t GROUP BY county"
    )
    results = [Result(id=0, sql=original, prompt="generator prompt")]
    state = _fact_state()

    await rewrite(results, state)

    assert len(model.requests) == 1
    assert results[0].sql == (
        "SELECT d.county_value, SUM(t.uplink_traffic) FROM "
        "(SELECT county, uplink_traffic FROM fact_metric WHERE time > 0) t "
        "INNER JOIN dim_exp_county d ON t.county = d.county_key GROUP BY d.county_value"
    )
    assert results[0].need_ref is False


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

    # Generator round: the candidate projects ``county`` only (deterministic path).
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
    must send exactly that system/user pair. Ordinal GROUP BY forces the LLM
    path so a request exists to pin.
    """
    model = scripted_model(f"```sql\n{REWRITTEN_SQL}\n```")
    rewrite = dimension_join_rewriter(_mappings("business_twin"), model, "postgres")
    results = [Result(id=0, sql=ORDINAL_BARE_SQL, prompt="original generator prompt")]
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
    assert ORDINAL_BARE_SQL in user_message.content


# --- Deterministic rewriter unit tests (ported from test_dimension_join_rewrite.py)


MAPPINGS = _mappings("business_twin")


def _detect(sql: str) -> tuple[exp.Expression, list[str], list[str]]:
    statement = sqlglot.parse_one(sql, read="postgres")
    projected = _projected_dimension_columns(statement, MAPPINGS)
    lost = _grouped_dimension_columns(statement, MAPPINGS, set(projected), _source_tables(statement))
    return statement, projected, lost


def _rewrite(sql: str):
    statement, projected, lost = _detect(sql)
    return _rewrite_dimensions_with_sqlglot(statement, projected + lost, MAPPINGS, "postgres")


def test_backfills_lost_dimension_and_qualifies_fact_references() -> None:
    statement, projected, lost = _detect(LOST_DIMENSION_SQL)

    assert projected == []
    assert lost == ["sub_app_id"]
    rewritten, processed = _rewrite_dimensions_with_sqlglot(statement, lost, MAPPINGS, "postgres")

    assert processed == ["sub_app_id"]
    assert rewritten == (
        "SELECT ne_name, dim_exp_subapp.sub_app_value AS sub_app_id, SUM(kpi_value) AS kpi_value "
        "FROM fact_5g_user_experience INNER JOIN dim_exp_subapp "
        "ON fact_5g_user_experience.sub_app_id = dim_exp_subapp.sub_app_id "
        "WHERE time >= 100 AND time < 200 AND fact_5g_user_experience.sub_app_id IN ('app1', 'app2') "
        "GROUP BY ne_name, dim_exp_subapp.sub_app_value"
    )
    assert _dimension_postconditions(statement, sqlglot.parse_one(rewritten, read="postgres"), processed, MAPPINGS)


def test_rewrites_multiple_dimensions_with_fact_alias() -> None:
    sql = (
        "SELECT f.time, f.city, f.county, SUM(f.uplink_traffic) AS uplink_traffic FROM fact_metric f "
        "WHERE f.county = 10 GROUP BY f.time, f.city, f.county"
    )

    rewritten, processed = _rewrite(sql)

    assert processed == ["city", "county"]
    assert rewritten == (
        "SELECT f.time, dim_exp_city.city_value AS city, dim_exp_county.county_value AS county, "
        "SUM(f.uplink_traffic) AS uplink_traffic FROM fact_metric AS f "
        "INNER JOIN dim_exp_city ON f.city = dim_exp_city.city_key "
        "INNER JOIN dim_exp_county ON f.county = dim_exp_county.county_key "
        "WHERE f.county = 10 GROUP BY f.time, dim_exp_city.city_value, dim_exp_county.county_value"
    )


def test_subquery_bare_reference_keeps_its_own_scope() -> None:
    """A same-named bare column inside a subquery belongs to that subquery, not the fact."""
    sql = (
        "SELECT time, county, SUM(x) FROM fact_metric "
        "WHERE cell_id IN (SELECT cell_id FROM t WHERE county = 'A') GROUP BY time, county"
    )

    statement, projected, lost = _detect(sql)
    rewritten, processed = _rewrite_dimensions_with_sqlglot(statement, projected + lost, MAPPINGS, "postgres")

    assert processed == ["county"]
    assert rewritten == (
        "SELECT time, dim_exp_county.county_value AS county, SUM(x) FROM fact_metric "
        "INNER JOIN dim_exp_county ON fact_metric.county = dim_exp_county.county_key "
        "WHERE cell_id IN (SELECT cell_id FROM t WHERE county = 'A') "
        "GROUP BY time, dim_exp_county.county_value"
    )
    assert _dimension_postconditions(statement, sqlglot.parse_one(rewritten, read="postgres"), processed, MAPPINGS)


def test_order_only_dimension_is_not_backfilled() -> None:
    """ORDER BY alone must not grow the projection or add a row-losing join."""
    sql = "SELECT time FROM fact_metric ORDER BY county"

    statement, projected, lost = _detect(sql)

    assert projected == []
    assert lost == []
    assert _rewrite_dimensions_with_sqlglot(statement, projected + lost, MAPPINGS, "postgres") is None


def test_other_table_qualified_column_is_not_a_fact_dimension() -> None:
    """A column qualified with another joined table is that table's, not the fact's."""
    sql = "SELECT t2.county, SUM(x) FROM fact_metric JOIN t2 ON fact_metric.k = t2.k GROUP BY t2.county"

    statement, projected, lost = _detect(sql)

    assert projected == []
    assert lost == []
    assert _rewrite_dimensions_with_sqlglot(statement, projected + lost, MAPPINGS, "postgres") is None


def test_order_by_other_table_column_is_preserved() -> None:
    """Sorting by another table's same-named column must keep pointing at that table."""
    sql = "SELECT county, SUM(x) FROM fact_metric JOIN t2 ON fact_metric.k = t2.k GROUP BY county ORDER BY t2.county"

    statement, _, _ = _detect(sql)
    rewritten, processed = _rewrite(sql)

    assert processed == ["county"]
    assert rewritten == (
        "SELECT dim_exp_county.county_value AS county, SUM(x) FROM fact_metric "
        "JOIN t2 ON fact_metric.k = t2.k "
        "INNER JOIN dim_exp_county ON fact_metric.county = dim_exp_county.county_key "
        "GROUP BY dim_exp_county.county_value ORDER BY t2.county"
    )


def test_grouped_fact_dimension_with_other_table_projection_falls_back() -> None:
    """Backfilling next to another table's same-named column must fail postconditions."""
    sql = "SELECT t2.county FROM fact_metric JOIN t2 ON fact_metric.k = t2.k GROUP BY county"

    statement, _, lost = _detect(sql)

    assert lost == ["county"]
    rewritten, processed = _rewrite(sql)
    assert "t2.county" in rewritten
    assert not _dimension_postconditions(statement, sqlglot.parse_one(rewritten, read="postgres"), processed, MAPPINGS)


def test_postconditions_reject_ambiguous_bare_reference() -> None:
    """A bare fact-key reference is ambiguous once the same-named lookup key is joined."""
    statement, _, lost = _detect(LOST_DIMENSION_SQL)
    ambiguous = (
        "SELECT ne_name, dim_exp_subapp.sub_app_value, SUM(kpi_value) AS kpi_value "
        "FROM fact_5g_user_experience INNER JOIN dim_exp_subapp "
        "ON fact_5g_user_experience.sub_app_id = dim_exp_subapp.sub_app_id "
        "WHERE time >= 100 AND time < 200 AND sub_app_id IN ('app1', 'app2') "
        "GROUP BY ne_name, dim_exp_subapp.sub_app_value"
    )

    assert not _dimension_postconditions(statement, sqlglot.parse_one(ambiguous, read="postgres"), lost, MAPPINGS)


def test_postconditions_reject_predicate_moved_onto_value_column() -> None:
    sql = (
        "SELECT time, county, SUM(uplink_traffic) AS uplink_traffic FROM fact_metric "
        "WHERE county = 10 GROUP BY time, county"
    )
    statement, projected, _ = _detect(sql)
    moved = (
        "SELECT time, dim_exp_county.county_value AS county, SUM(uplink_traffic) AS uplink_traffic "
        "FROM fact_metric INNER JOIN dim_exp_county ON fact_metric.county = dim_exp_county.county_key "
        "WHERE dim_exp_county.county_value = 10 GROUP BY time, dim_exp_county.county_value"
    )

    assert not _dimension_postconditions(statement, sqlglot.parse_one(moved, read="postgres"), projected, MAPPINGS)


def test_postconditions_accept_qualified_llm_style_rewrite() -> None:
    """Qualification differences alone must not reject an otherwise complete rewrite."""
    sql = (
        "SELECT time, county, SUM(uplink_traffic) AS uplink_traffic FROM fact_metric "
        "WHERE county = 10 GROUP BY time, county"
    )
    statement, projected, _ = _detect(sql)
    qualified = (
        "SELECT f.time, d.county_value, SUM(f.uplink_traffic) AS uplink_traffic FROM fact_metric f "
        "INNER JOIN dim_exp_county d ON f.county = d.county_key "
        "WHERE f.county = 10 GROUP BY f.time, d.county_value"
    )

    assert _dimension_postconditions(statement, sqlglot.parse_one(qualified, read="postgres"), projected, MAPPINGS)
