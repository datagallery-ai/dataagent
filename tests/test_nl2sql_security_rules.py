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
"""Unit tests for the nl2sql plugin's SQL security rules.

Pins the security behaviors ported from the ``br_release_1230`` lineage: the
GROUP BY modifier allowlist (SYNTAX-001), the scope-aware unfiltered-row
detection (RESOURCE-009, a WHERE inside a subquery does not filter an outer
base-table read), the grouped-aggregate exemption that keeps plain
``GROUP BY`` aggregates out of the RESOURCE-009 net, and the 1230 security
completion: grouped-projection rejection, the single-row-aggregate ON TRUE
join exemption, iterative constant-boolean evaluation with RESOURCE-002
fail-closed depth protection, and the column-validation policy switch.
"""

import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = PROJECT / "dataagent/plugins" / "nl2sql"

# Direct ``nl2sql`` imports need the plugin root importable, like the other
# plugin-level unit tests; the host package resolves from the project root.
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from nl2sql.security import check_sql  # noqa: E402
from nl2sql.security import rules as security_rules  # noqa: E402

SCHEMA = {"orders": {"columns": {"id": {}}}, "customers": {"columns": {"id": {}}}}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT department, COUNT(*) AS cnt FROM employees WHERE id > 0 GROUP BY ROLLUP(department)",
        "SELECT department, COUNT(*) AS cnt FROM employees WHERE id > 0 GROUP BY CUBE(department)",
        "SELECT department, seniority, COUNT(*) AS cnt FROM employees WHERE id > 0 GROUP BY GROUPING SETS ((department), (seniority))",
    ],
    ids=["rollup", "cube", "grouping-sets"],
)
def test_check_sql_rejects_group_by_modifier_expressions(sql: str) -> None:
    """ROLLUP / CUBE / GROUPING SETS are not in the GROUP BY allowlist.

    The WHERE keeps the resource check quiet so the syntax allowlist is what fires.
    """
    schema = {"employees": {"columns": {"id": {}, "department": {}, "seniority": {}}}}

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["SYNTAX-001"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id, (SELECT 1 FROM customers WHERE id = 1) FROM orders",
        "SELECT o.id FROM orders o, (SELECT 1 AS x FROM customers WHERE id = 1) s",
        "SELECT id FROM orders CROSS JOIN (SELECT 1 AS x FROM customers WHERE id = 1) s",
        "SELECT id FROM orders o, (SELECT 1 FROM customers WHERE id = 1) s UNION ALL SELECT 1",
    ],
)
def test_check_sql_rejects_outer_base_read_hidden_by_subquery_where(sql: str) -> None:
    """A WHERE inside a subquery does not filter an outer base-table read."""
    result = check_sql(sql, dialect="postgres", schema=SCHEMA)

    assert result.blocked is True
    assert "RESOURCE-009" in [violation.rule_id for violation in result.violations]


@pytest.mark.parametrize(
    "sql",
    [
        "WITH recent AS (SELECT id FROM orders WHERE id = 1) SELECT id FROM recent",
        "SELECT id FROM (SELECT id FROM orders WHERE id = 1) AS recent",
        "WITH recent AS (SELECT id FROM orders) SELECT id FROM recent WHERE id = 1",
        "SELECT id FROM (SELECT id FROM orders) AS recent LIMIT 10",
        "SELECT id FROM orders WHERE id = 1",
    ],
)
def test_check_sql_allows_filtered_base_reads_inside_query_tree(sql: str) -> None:
    """Outer CTE and subquery projections should not be mistaken for base-table scans."""
    result = check_sql(sql, dialect="postgres", schema=SCHEMA)

    assert result.blocked is False


def test_check_sql_exempts_grouped_aggregate_without_where() -> None:
    """A plain GROUP BY aggregate collapses rows, so it is not an unfiltered dump."""
    schema = {"employees": {"columns": {"department": {}}}}

    result = check_sql(
        "SELECT department, COUNT(*) AS cnt FROM employees GROUP BY department",
        dialect="postgres",
        schema=schema,
    )

    assert result.violations == []


def test_check_sql_still_flags_unfiltered_plain_select() -> None:
    """Without any row restriction a bare SELECT over a base table stays blocked."""
    result = check_sql("SELECT id FROM orders", dialect="postgres", schema=SCHEMA)

    assert result.blocked is True
    assert "RESOURCE-009" in [violation.rule_id for violation in result.violations]


def test_check_sql_exempts_single_row_aggregate() -> None:
    """An ungrouped aggregate with every column inside the function is one row."""
    result = check_sql("SELECT COUNT(*) FROM orders", dialect="postgres", schema=SCHEMA)

    assert result.violations == []


def test_check_sql_rejects_selected_column_missing_from_group_by() -> None:
    """A selected column under GROUP BY must be grouped or aggregated."""
    bare = check_sql(
        "SELECT time, crh_unsub_users FROM fact_metric WHERE time > 0 GROUP BY time",
        dialect="postgres",
        schema={},
    )
    summed = check_sql(
        "SELECT time, SUM(crh_unsub_users) AS crh_unsub_users FROM fact_metric WHERE time > 0 GROUP BY time",
        dialect="postgres",
        schema={"fact_metric": {"columns": {"time": {}, "crh_unsub_users": {}}}},
    )

    assert bare.blocked is True
    assert bare.violations[0].rule_id == "SYNTAX-001"
    assert "crh_unsub_users" in bare.violations[0].message
    assert summed.blocked is False


def test_check_sql_allows_generate_series_joined_to_modeled_table() -> None:
    """The generate_series table function should remain subject to the semantic-table requirement."""
    schema = {"orders": {"columns": {"id": {}}}}
    sql = (
        "SELECT orders.id FROM orders CROSS JOIN generate_series(1, 2) AS value WHERE orders.id = 1"
    )

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False


def test_check_sql_allows_on_true_join_between_single_row_aggregate_ctes() -> None:
    """ON TRUE should be safe when both CTE sources are guaranteed to return at most one row."""
    table_name = "fact_dw1745159016_0000000000000020_metric_1d"
    columns = dict.fromkeys(("trigger_assurance_users", "term_brand", "time"), {})
    schema = {table_name: {"columns": columns}}
    sql = (
        "WITH current_period AS ("
        "SELECT SUM(trigger_assurance_users) AS current_value "
        f"FROM {table_name} WHERE term_brand = 1 AND time >= 100 AND time < 200"
        "), comparison_period AS ("
        "SELECT SUM(trigger_assurance_users) AS comparison_value "
        f"FROM {table_name} WHERE term_brand = 1 AND time >= 0 AND time < 100"
        ") SELECT cp.current_value, cpm.comparison_value, "
        "(cp.current_value::numeric - cpm.comparison_value::numeric) "
        "/ NULLIF(cpm.comparison_value::numeric, 0) AS change_rate "
        "FROM current_period cp FULL OUTER JOIN comparison_period cpm ON TRUE"
    )

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False


def test_check_sql_rejects_on_true_when_aggregate_belongs_to_nested_select() -> None:
    """A scalar aggregate nested in a row-returning CTE must not make that CTE single-row."""
    schema = {"orders": {"columns": {"id": {}, "amount": {}}}}
    sql = (
        "WITH row_period AS ("
        "SELECT (SELECT SUM(i.amount) FROM orders i WHERE i.id = 1) AS current_value "
        "FROM orders o WHERE o.id > 0"
        "), grouped_period AS ("
        "SELECT id, SUM(amount) AS comparison_value FROM orders WHERE id > 0 GROUP BY id"
        ") SELECT rp.current_value, gp.comparison_value "
        "FROM row_period rp FULL OUTER JOIN grouped_period gp ON TRUE"
    )

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["RESOURCE-007"]


def test_check_sql_allows_on_true_join_with_one_single_row_aggregate_cte() -> None:
    """One proven single-row CTE should prevent multiplicative ON TRUE expansion."""
    schema = {"orders": {"columns": {"id": {}, "amount": {}}}}
    sql = (
        "WITH grouped_period AS ("
        "SELECT id, SUM(amount) AS current_value FROM orders WHERE id > 0 GROUP BY id"
        "), comparison_period AS ("
        "SELECT SUM(amount) AS comparison_value FROM orders WHERE id = 2"
        ") SELECT gp.current_value, cp.comparison_value "
        "FROM grouped_period gp FULL OUTER JOIN comparison_period cp ON TRUE"
    )

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False


def test_check_sql_allows_on_true_join_between_table_and_single_row_aggregate_cte() -> None:
    """A single-row aggregate CTE should be safe to attach to filtered base rows with ON TRUE."""
    schema = {"orders": {"columns": {"id": {}, "amount": {}}}}
    sql = (
        "WITH totals AS (SELECT SUM(amount) AS total FROM orders WHERE id > 0) "
        "SELECT o.id, t.total FROM orders o JOIN totals t ON TRUE WHERE o.id > 0"
    )

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False


def test_check_sql_rejects_on_true_join_when_aggregate_cte_expands_rows() -> None:
    """A set-returning projection must not make an aggregate CTE look single-row."""
    schema = {"orders": {"columns": {"id": {}, "amount": {}}}}
    sql = (
        "WITH expanded_period AS ("
        "SELECT SUM(amount) AS current_value, generate_series(1, 100) AS position FROM orders WHERE id > 0"
        "), grouped_period AS ("
        "SELECT id, SUM(amount) AS comparison_value FROM orders WHERE id > 0 GROUP BY id"
        ") SELECT ep.current_value, gp.comparison_value "
        "FROM expanded_period ep FULL OUTER JOIN grouped_period gp ON TRUE"
    )

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["RESOURCE-007"]


def test_flat_and_chain_is_resource_008() -> None:
    """A flat always-true chain evaluates iteratively and reports RESOURCE-008, not a depth failure."""
    cond = " AND ".join(["1=1"] * 1200)
    result = check_sql(
        f"SELECT id FROM orders WHERE {cond}",
        dialect="postgres",
        schema={"orders": {"columns": {"id": {}}}},
    )

    assert result.blocked is True
    assert "RESOURCE-008" in [item.rule_id for item in result.violations]


def test_deeply_nested_boolean_fails_closed() -> None:
    """A pathological nested-boolean expression must fail closed instead of crashing."""
    cond = "(" * 2000 + "1=1" + ")" * 2000
    result = check_sql(
        f"SELECT id FROM orders WHERE {cond}",
        dialect="postgres",
        schema={"orders": {"columns": {"id": {}}}},
    )

    assert result.blocked is True


def test_check_sql_allows_column_missing_from_semantic_schema() -> None:
    """Security checker should leave source-column existence to the database."""
    schema = {"orders": {"columns": {"id": {"value_type": "integer"}}}}

    result = check_sql(
        "SELECT password FROM orders WHERE id = 1", dialect="postgres", schema=schema
    )

    assert result.blocked is False
    assert result.violations == []


def test_check_sql_can_restore_missing_column_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retained column-validation path should still emit SCHEMA-002 when explicitly enabled."""
    monkeypatch.setattr(security_rules, "_SEMANTIC_COLUMN_VALIDATION_ENABLED", True)
    schema = {"orders": {"columns": {"id": {}}}}

    result = check_sql(
        "SELECT password FROM orders WHERE id = 1", dialect="postgres", schema=schema
    )

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["SCHEMA-002"]
