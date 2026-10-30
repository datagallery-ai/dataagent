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
import sqlglot
from sqlglot import exp

from dataagent.agents.nl2sql.utils.nl2sql_utils import (
    _dimension_postconditions,
    _grouped_dimension_columns,
    _projected_dimension_columns,
    _rewrite_dimensions_with_sqlglot,
    _source_tables,
    load_dimension_mappings,
)

MAPPINGS = load_dimension_mappings("business_twin")

# The real lost-dimension shape: sub_app_id is grouped and filtered but never projected,
# and the lookup key shares the fact column's name.
LOST_DIMENSION_SQL = (
    "SELECT ne_name, SUM(kpi_value) AS kpi_value FROM fact_5g_user_experience "
    "WHERE time >= 100 AND time < 200 AND sub_app_id IN ('app1', 'app2') "
    "GROUP BY ne_name, sub_app_id"
)


def _detect(sql: str) -> tuple[exp.Expression, list[str], list[str]]:
    statement = sqlglot.parse_one(sql, read="postgres")
    projected = _projected_dimension_columns(statement, MAPPINGS)
    lost = _grouped_dimension_columns(statement, MAPPINGS, set(projected), _source_tables(statement))
    return statement, projected, lost


def _rewrite(sql: str):
    statement, projected, lost = _detect(sql)
    return _rewrite_dimensions_with_sqlglot(statement, projected + lost, MAPPINGS, "postgres")


def test_detects_dimension_grouped_but_not_projected() -> None:
    _, projected, lost = _detect(LOST_DIMENSION_SQL)

    assert projected == []
    assert lost == ["sub_app_id"]


def test_backfills_lost_dimension_and_qualifies_fact_references() -> None:
    statement, _, lost = _detect(LOST_DIMENSION_SQL)

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


def test_replaces_projected_dimension_preserving_alias_and_clauses() -> None:
    sql = (
        "SELECT time, county, SUM(uplink_traffic) AS uplink_traffic FROM fact_metric "
        "WHERE county = 10 GROUP BY time, county ORDER BY county"
    )

    rewritten, processed = _rewrite(sql)

    assert processed == ["county"]
    assert rewritten == (
        "SELECT time, dim_exp_county.county_value AS county, SUM(uplink_traffic) AS uplink_traffic "
        "FROM fact_metric INNER JOIN dim_exp_county ON fact_metric.county = dim_exp_county.county_key "
        "WHERE fact_metric.county = 10 GROUP BY time, dim_exp_county.county_value "
        "ORDER BY dim_exp_county.county_value"
    )


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


def test_guards_decline_unsafe_shapes() -> None:
    assert _rewrite("SELECT county, SUM(uplink_traffic) FROM fact_metric WHERE time > 0 GROUP BY 1") is None
    assert _rewrite("SELECT county, SUM(uplink_traffic) FROM (SELECT * FROM fact_metric) t GROUP BY county") is None


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


def test_already_joined_sql_has_nothing_to_do() -> None:
    sql = (
        "SELECT time, dim_exp_county.county_value AS county, SUM(uplink_traffic) AS uplink_traffic "
        "FROM fact_metric INNER JOIN dim_exp_county ON fact_metric.county = dim_exp_county.county_key "
        "WHERE fact_metric.county = 10 GROUP BY time, dim_exp_county.county_value ORDER BY county"
    )

    statement, projected, lost = _detect(sql)

    assert projected == []
    assert lost == []
    assert _rewrite_dimensions_with_sqlglot(statement, [], MAPPINGS, "postgres") is None


def test_postconditions_reject_half_applied_rewrite() -> None:
    """Joining and regrouping while still projecting the bare fact column must fail."""
    statement, _, lost = _detect(LOST_DIMENSION_SQL)
    half_rewritten = (
        "SELECT ne_name, sub_app_id, SUM(kpi_value) AS kpi_value FROM fact_5g_user_experience "
        "INNER JOIN dim_exp_subapp ON fact_5g_user_experience.sub_app_id = dim_exp_subapp.sub_app_id "
        "WHERE time >= 100 AND time < 200 GROUP BY ne_name, dim_exp_subapp.sub_app_value"
    )

    assert not _dimension_postconditions(statement, sqlglot.parse_one(half_rewritten, read="postgres"), lost, MAPPINGS)


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
