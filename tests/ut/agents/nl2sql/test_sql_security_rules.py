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
"""AST security rules R1–R3 (hard reject) wired through guard_sql.

Bounded recursive CTE policy (documented):
- WITH RECURSIVE triggers the check.
- Allow only when a common upper-bound pattern is recognized on the recursive
  member: numeric LT/LTE in WHERE that references a recursive/iteration column
  (not bare literal vs literal like ``1 < 999999``), LIMIT on that member, or a
  dialect max-recursion hint node. Outer-query LIMIT alone is NOT accepted
  (false safety).
- Otherwise default-deny with a rewrite suggestion.
"""

from __future__ import annotations

import pytest

from dataagent.agents.nl2sql.utils.sql_guard import SQLGuardError, guard_sql
from dataagent.agents.nl2sql.utils.sql_rules import DEFAULT_RULES, Violation


def _raise_rule(sql: str, rule_id: str, *, dialect: str | None = "postgres") -> SQLGuardError:
    with pytest.raises(SQLGuardError) as ei:
        guard_sql(sql, dialect=dialect)
    err = ei.value
    assert err.violations, f"expected structured violations, got: {err}"
    ids = {v.rule_id for v in err.violations}
    assert rule_id in ids, f"expected {rule_id} in {ids}; violations={err.violations}"
    hit = next(v for v in err.violations if v.rule_id == rule_id)
    assert hit.message
    assert hit.fragment
    assert hit.suggestion
    return err


def test_r1_rejects_cross_join():
    _raise_rule("SELECT * FROM a CROSS JOIN b", "R1")


def test_r2_rejects_bare_join_without_on_or_using():
    _raise_rule("SELECT * FROM a JOIN b", "R2")


def test_r2_rejects_natural_join_by_default():
    _raise_rule("SELECT * FROM a NATURAL JOIN b", "R2")


def test_r2_rejects_comma_join_without_join_condition():
    _raise_rule("SELECT * FROM a, b", "R2")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM a JOIN b ON TRUE",
        "SELECT * FROM a JOIN b ON 1=1",
        "SELECT * FROM a JOIN b ON 1",
        "SELECT * FROM a INNER JOIN b ON (TRUE)",
        "SELECT * FROM a LEFT JOIN b ON 1 = 1",
        "SELECT * FROM a JOIN b ON 'a' = 'a'",
        "SELECT * FROM a JOIN b ON 2 > 1",
        "SELECT * FROM a JOIN b ON NOT FALSE",
        "SELECT * FROM a JOIN b ON TRUE OR FALSE",
        "SELECT * FROM a JOIN b ON ('x' = 'x')",
    ],
)
def test_r1_rejects_join_on_tautology_as_cartesian(sql: str):
    """ON TRUE / 1=1 / 1 / string-EQ / comparisons / NOT FALSE / OR is cartesian."""
    _raise_rule(sql, "R1")


def test_r3_rejects_unbounded_recursive_cte():
    sql = "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t) SELECT * FROM t"
    _raise_rule(sql, "R3")


def test_r3_rejects_when_only_outer_limit_present():
    """Outer LIMIT does not bound CTE materialization — still R3."""
    sql = "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t) SELECT * FROM t LIMIT 10"
    _raise_rule(sql, "R3")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM (SELECT * FROM a CROSS JOIN b) t",
        "WITH x AS (SELECT * FROM a CROSS JOIN b) SELECT * FROM x",
        "SELECT * FROM a WHERE id IN (SELECT id FROM b CROSS JOIN c)",
        "SELECT * FROM a JOIN b ON a.id = b.id UNION SELECT * FROM c CROSS JOIN d",
    ],
)
def test_r1_hits_nested_cte_and_union(sql: str):
    _raise_rule(sql, "R1")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM (SELECT * FROM a JOIN b) t",
        "WITH x AS (SELECT * FROM a JOIN b) SELECT * FROM x",
        "SELECT 1 WHERE EXISTS (SELECT 1 FROM a NATURAL JOIN b)",
        "SELECT * FROM a JOIN b ON a.id = b.id UNION ALL SELECT * FROM c, d",
    ],
)
def test_r2_hits_nested_cte_and_union(sql: str):
    _raise_rule(sql, "R2")


def test_r3_hits_recursive_cte_inside_subquery():
    sql = "SELECT * FROM (WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t) SELECT * FROM t) s"
    _raise_rule(sql, "R3")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM a JOIN b ON a.id = b.id",
        "SELECT * FROM a INNER JOIN b ON a.id = b.id",
        "SELECT * FROM a LEFT JOIN b ON a.id = b.id",
        "SELECT * FROM a JOIN b USING (id)",
        "SELECT a.id FROM a JOIN b ON a.id = b.id WHERE a.id > 0",
    ],
)
def test_allows_proper_joins(sql: str):
    guard_sql(sql, dialect="postgres")


@pytest.mark.parametrize(
    "sql",
    [
        # WHERE upper bound must reference recursive/iteration column
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 10) SELECT * FROM t",
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE 10 > n) SELECT * FROM t",
        # Simple arithmetic wrapping the recursive column still counts as a bound.
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n + 0 < 10) SELECT * FROM t",
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE (n + 1) <= 10) SELECT * FROM t",
        # LIMIT on recursive member
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t LIMIT 10) SELECT * FROM t",
    ],
)
def test_allows_bounded_recursive_cte_when_recognizable(sql: str):
    guard_sql(sql, dialect="postgres")


def test_r3_rejects_fake_literal_only_bound():
    """WHERE 1 < 999999 does not reference the recursive column — not a bound."""
    sql = "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE 1 < 999999) SELECT * FROM t"
    _raise_rule(sql, "R3")


def test_violation_is_structured_dataclass_fields():
    with pytest.raises(SQLGuardError) as ei:
        guard_sql("SELECT * FROM a CROSS JOIN b", dialect="postgres")
    v = ei.value.violations[0]
    assert isinstance(v, Violation)
    assert v.rule_id == "R1"
    assert "CROSS" in v.fragment.upper() or "cross" in v.message.lower()
    assert v.suggestion


def test_builtin_rules_list_is_extensible():
    """New rules append to DEFAULT_RULES with the same Rule(rule_id, check) shape."""
    ids = [r.rule_id for r in DEFAULT_RULES]
    assert ids == ["R1", "R2", "R3"]
    assert "R4" not in ids


def test_mysql_dialect_cross_join():
    _raise_rule("SELECT * FROM a CROSS JOIN b", "R1", dialect="mysql")
