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
"""Snapshot test: every public SQL-security message must match a reviewed whitelist.

Public messages are fixed text (raw identifiers live in the server-side detail
field). A battery of blocking queries exercises every rule family; any message
that is not in the whitelist — e.g. a new f-string embedding a table or column
name — fails this snapshot and must be reviewed before it can ship.
"""

import re
from collections.abc import Iterator

import pytest

from dataagent.agents.nl2sql.security import check_sql
from dataagent.agents.nl2sql.security import rules as security_rules
from dataagent.agents.nl2sql.security.models import SecurityViolation

# Exact fixed texts each rule may emit. Keep in sync with checker.py / rules.py.
_FIXED_PUBLIC_MESSAGES: dict[str, set[str]] = {
    "SQL-001": {
        "SQL parse failed.",
        "SQL normalization failed.",
        "Exactly one SQL statement is allowed.",
        "Only read-only SELECT queries are allowed.",
        "Every SELECT must contain a projection.",
    },
    "SQL-002": {
        "Write, DDL, control, and maintenance operations are not allowed.",
        "SELECT INTO is not allowed.",
        "Row-locking SELECT queries are not allowed.",
    },
    "SYNTAX-001": {
        "Only SELECT and UNION query roots are in the syntax allowlist.",
        "UNION modifier is not in the syntax allowlist.",
        "Only IS NULL and IS NOT NULL predicates are allowed.",
        "LIKE pattern must be a string literal.",
        "LIKE pattern must contain non-wildcard text.",
        "JOIN type is not in the allowlist.",
        "CROSS JOIN must not contain an ON condition.",
        "JOIN modifier is not in the syntax allowlist.",
        "Only plain DISTINCT is in the syntax allowlist.",
        "GROUP BY modifier is not in the allowlist.",
        "ORDER SIBLINGS BY is not in the syntax allowlist.",
        "WITH modifier is not in the syntax allowlist.",
        "CTE modifier is not in the syntax allowlist.",
        "LIMIT modifier is not in the syntax allowlist.",
        (
            "A selected column is not in GROUP BY and is not aggregated, so the query cannot "
            "execute. Wrap a metric in SUM(), or add a dimension to GROUP BY."
        ),
    },
    "RESOURCE-001": {"SQL text exceeds 1 MiB."},
    "RESOURCE-002": {
        "SQL AST exceeds 10,000 nodes.",
        "SQL boolean expression exceeds evaluation depth.",
    },
    "RESOURCE-003": {"IN list exceeds 50,000 expressions."},
    "RESOURCE-007": {
        "JOIN requires ON or USING unless it is a CROSS JOIN.",
        "JOIN condition must not be always true. Use CROSS JOIN when a Cartesian product is intended.",
    },
    "RESOURCE-008": {"WHERE or HAVING condition is always true."},
    "RESOURCE-009": {"Unfiltered row query requires WHERE, HAVING, LIMIT, or FETCH."},
    "SCHEMA-001": {"Source table cannot be resolved uniquely in the provided semantic schema."},
    "SCHEMA-002": {
        (
            "Column is not exposed by a source available in this query scope. Verify that the column exists in "
            "the referenced physical table's semantic schema or is included in the referenced CTE/subquery "
            "SELECT list. Then use the actual exposed column name with the correct table or CTE alias."
        )
    },
    "SCHEMA-003": {
        (
            "Query does not reference a table from the provided semantic schema. Regenerate it with at least one "
            "modeled business table and use only that table's exposed columns."
        )
    },
    "SCHEMA-004": {
        (
            "Column reference is ambiguous in the current query scope because multiple sources expose it. "
            "This is not a missing semantic-schema column. Qualify every unqualified reference with the "
            "intended source alias."
        )
    },
}

# Rules whose message embeds exactly one generic SQL token (an allowlisted-family
# function name, an lpad/rpad spelling, a sqlglot node type, or a SELECT clause
# arg name). Anything beyond that single token fails the snapshot.
_PARAMETERIZED_PUBLIC_MESSAGES: dict[str, list[re.Pattern[str]]] = {
    "FUNCTION-001": [re.compile(r"^SQL function is not in the allowlist: [A-Za-z0-9_.()]+\.$")],
    "RESOURCE-006": [re.compile(r"^(?:lpad|rpad) output is unknown or exceeds 16 MiB\.$")],
    "SYNTAX-001": [
        re.compile(r"^SQL syntax is not in the allowlist: [A-Za-z]+\.$"),
        re.compile(r"^SELECT clause is not in the allowlist: [a-z_]+\.$"),
    ],
}

_SCHEMA_ORDERS = {"orders": {"columns": {"id": {}, "name": {}, "amount": {}, "status": {}, "customer_id": {}}}}
_SCHEMA_AB = {
    "a": {"columns": {"id": {}}},
    "b": {"columns": {"id": {}}},
    "customers": {"columns": {"id": {}}},
    **_SCHEMA_ORDERS,
}

# (sql, schema, column_validation_enabled) battery. Every entry mirrors an input
# proven by test_sql_security_checker.py, so the produced rule and message are
# deterministic.
_BATTERY: list[tuple[str, dict, bool]] = [
    ("DELETE FROM users", {}, False),
    ("SELECT 1; SELECT 2", {}, False),
    ("SELECT", {}, False),
    ("SELECT * INTO copied_orders FROM orders", _SCHEMA_ORDERS, False),
    ("SELECT id FROM orders FOR UPDATE", _SCHEMA_ORDERS, False),
    ("SELECT MD5('a') FROM orders WHERE id = 1", _SCHEMA_ORDERS, False),
    ("SELECT pg_catalog.COALESCE(NULL, 0)", {}, False),
    ("SELECT 1 EXCEPT SELECT 2", _SCHEMA_ORDERS, False),
    ("SELECT id FROM orders WHERE name ILIKE 'A%'", _SCHEMA_ORDERS, False),
    ("SELECT id FROM orders FETCH FIRST 10 ROWS ONLY", _SCHEMA_ORDERS, False),
    ("SELECT time, crh_unsub_users FROM fact_metric WHERE time > 0 GROUP BY time", {}, False),
    ("SELECT id FROM orders WHERE name LIKE '%'", _SCHEMA_ORDERS, False),
    ("SELECT id FROM orders WHERE name LIKE pattern", _SCHEMA_ORDERS, False),
    ("SELECT amount IS TRUE FROM orders WHERE id = 1", _SCHEMA_ORDERS, False),
    ("SELECT DISTINCT ON (id) id FROM orders WHERE id = 1", _SCHEMA_ORDERS, False),
    ("SELECT id FROM orders WHERE id BETWEEN 1 AND 10", _SCHEMA_ORDERS, False),
    (
        "SELECT orders.id FROM orders SEMI JOIN customers ON orders.customer_id = customers.id WHERE orders.id = 1",
        _SCHEMA_AB,
        False,
    ),
    (
        "SELECT orders.id FROM orders CROSS JOIN customers ON orders.customer_id = customers.id WHERE orders.id = 1",
        _SCHEMA_AB,
        False,
    ),
    (
        "SELECT customer_id, SUM(amount) FROM orders WHERE customer_id = 1 GROUP BY ROLLUP (customer_id)",
        _SCHEMA_ORDERS,
        False,
    ),
    ("WITH RECURSIVE x AS (SELECT 1) SELECT * FROM x", _SCHEMA_ORDERS, False),
    (
        "WITH recent AS MATERIALIZED (SELECT id FROM orders WHERE id = 1) SELECT id FROM recent",
        _SCHEMA_ORDERS,
        False,
    ),
    ("SELECT id FROM pg_authid WHERE id = 1", {}, False),
    ("SELECT 1", _SCHEMA_ORDERS, False),
    ("SELECT password FROM orders WHERE id = 1", {"orders": {"columns": {"id": {}}}}, True),
    (
        "SELECT id FROM orders CROSS JOIN customers WHERE orders.id = customers.id",
        {"orders": {"columns": {"id": {}}}, "customers": {"columns": {"id": {}}}},
        True,
    ),
    ("SELECT a.id FROM a JOIN b WHERE a.id = 1", _SCHEMA_AB, False),
    ("SELECT a.id FROM a JOIN b ON TRUE WHERE a.id = 1", _SCHEMA_AB, False),
    ("SELECT id FROM orders WHERE id = 1 OR TRUE", _SCHEMA_ORDERS, False),
    ("SELECT id FROM orders", _SCHEMA_ORDERS, False),
    ("SELECT lpad('x', unknown_size, 'y')", {}, False),
    ("SELECT '" + ("测" * 350_000) + "'", {}, False),
    ("SELECT " + ",".join("1" for _ in range(10_001)), {}, False),
    ("SELECT id FROM orders WHERE " + ("(" * 2000) + "1=1" + (")" * 2000), _SCHEMA_ORDERS, False),
]


def _iter_battery_violations() -> Iterator[SecurityViolation]:
    """Run the battery and yield every produced violation."""
    original_flag = security_rules._SEMANTIC_COLUMN_VALIDATION_ENABLED
    try:
        for sql, schema, column_validation in _BATTERY:
            security_rules._SEMANTIC_COLUMN_VALIDATION_ENABLED = column_validation
            result = check_sql(sql, dialect="postgres", schema=schema)
            if result.violations:
                yield from result.violations
    finally:
        security_rules._SEMANTIC_COLUMN_VALIDATION_ENABLED = original_flag


def _message_is_whitelisted(rule_id: str, message: str) -> bool:
    if message in _FIXED_PUBLIC_MESSAGES.get(rule_id, set()):
        return True
    return any(pattern.fullmatch(message) for pattern in _PARAMETERIZED_PUBLIC_MESSAGES.get(rule_id, []))


def test_battery_messages_match_the_public_whitelist() -> None:
    """Every message the battery produces must be reviewed fixed text."""
    violations = list(_iter_battery_violations())

    assert violations, "the battery must produce at least one violation"
    for violation in violations:
        assert _message_is_whitelisted(violation.rule_id, violation.message), (
            f"unreviewed public message for {violation.rule_id}: {violation.message!r} "
            "(add it to the whitelist or move the dynamic part into detail)"
        )


def test_battery_covers_every_whitelisted_rule() -> None:
    """Every whitelisted rule family must stay exercised by the battery."""
    produced_rules = {violation.rule_id for violation in _iter_battery_violations()}
    whitelisted_rules = set(_FIXED_PUBLIC_MESSAGES) | set(_PARAMETERIZED_PUBLIC_MESSAGES)

    assert produced_rules <= whitelisted_rules
    # RESOURCE-003 is shadowed by the 10,000-node AST limit: any IN list large
    # enough to trip it always trips RESOURCE-002 first.
    assert whitelisted_rules - produced_rules == {"RESOURCE-003"}
