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
import pytest

from dataagent.agents.nl2sql.security import check_sql


def test_check_sql_rejects_non_select_statement() -> None:
    """Security checker should reject non-query statements."""
    result = check_sql("DELETE FROM users", dialect="postgres", schema={})

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["SQL-001"]


def test_check_sql_rejects_multiple_statements() -> None:
    """Security checker should require exactly one executable statement."""
    result = check_sql("SELECT 1; SELECT 2", dialect="postgres", schema={})

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["SQL-001"]


def test_check_sql_rejects_select_without_projection() -> None:
    """Security checker should reject SQLGlot's permissive bare SELECT tree."""
    result = check_sql("SELECT", dialect="postgres", schema={})

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["SQL-001"]


@pytest.mark.parametrize("sql", ["SELECT * INTO copied_orders FROM orders", "SELECT id FROM orders FOR UPDATE"])
def test_check_sql_rejects_mutating_select_variants(sql: str) -> None:
    """Security checker should reject SELECT variants that write or lock data."""
    schema = {"orders": {"columns": {"id": {}}}}

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is True
    assert "SQL-002" in [violation.rule_id for violation in result.violations]


@pytest.mark.parametrize(
    "sql",
    [
        "CALL refresh_orders()",
        "DO $$ BEGIN NULL; END $$",
        "VACUUM orders",
        "ANALYZE orders",
        "SHOW search_path",
        "RESET search_path",
    ],
)
def test_check_sql_rejects_database_commands(sql: str) -> None:
    """Security checker should reject command roots even when SQLGlot models them generically."""
    result = check_sql(sql, dialect="postgres", schema={})

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["SQL-001"]


def test_check_sql_rejects_dangerous_metadata_function() -> None:
    """Security checker should reject database metadata functions."""
    schema = {"orders": {"columns": {"id": {}}}}

    result = check_sql(
        "SELECT current_setting('search_path') FROM orders WHERE id = 1",
        dialect="postgres",
        schema=schema,
    )

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["FUNCTION-001"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(1), COUNT(*), AVG(1), MAX(1), MIN(1) FROM orders WHERE id = 1",
        "SELECT COUNT(DISTINCT status) FROM orders WHERE id = 1",
        "SELECT NULLIF(1, 0), NVL(NULL, 0), COALESCE(NULL, 0) FROM orders WHERE id = 1",
        "SELECT ROUND(1.2), CEIL(1.2), CEILING(1.2), FLOOR(1.2), ABS(-1) FROM orders WHERE id = 1",
        "SELECT CAST('1' AS INTEGER), '1'::INTEGER, TO_NUMBER('1', '9'), "
        "TO_CHAR(CURRENT_DATE, 'YYYY') FROM orders WHERE id = 1",
        "SELECT TO_DATE('2024-01-01', 'YYYY-MM-DD'), HEX('a') FROM orders WHERE id = 1",
        "SELECT CONCAT('a', 'b'), CONCAT_WS('-', 'a', 'b'), SUBSTR('abc', 1, 2) FROM orders WHERE id = 1",
        "SELECT UPPER('a'), LOWER('A'), LENGTH('a'), LENGTHB('a') FROM orders WHERE id = 1",
        "SELECT TRIM(' a '), LTRIM(' a'), RTRIM('a '), REPLACE('a', 'a', 'b') FROM orders WHERE id = 1",
        "SELECT LPAD('1', 2, '0'), RPAD('1', 2, '0'), INSTR('abc', 'b') FROM orders WHERE id = 1",
        "SELECT NOW(), CURRENT_DATE, CURRENT_TIMESTAMP, EXTRACT(YEAR FROM CURRENT_DATE) FROM orders WHERE id = 1",
        "SELECT DATE_TRUNC('day', CURRENT_TIMESTAMP), CASE WHEN 1 = 1 THEN 1 ELSE 0 END FROM orders WHERE id = 1",
        "SELECT generate_series(1, 2) FROM orders WHERE id = 1",
    ],
)
def test_check_sql_allows_whitelisted_functions(sql: str) -> None:
    """Security checker should allow every explicitly whitelisted function spelling."""
    schema = {"orders": {"columns": {"id": {}, "status": {}}}}

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT MD5('a') FROM orders WHERE id = 1",
        "SELECT jsonb_array_elements('[1,2]'::jsonb) FROM orders WHERE id = 1",
        "SELECT repeat('a', 2) FROM orders WHERE id = 1",
        "SELECT IFNULL(NULL, 0) FROM orders WHERE id = 1",
        "SELECT SUBSTRING('abc' FROM 1 FOR 2) FROM orders WHERE id = 1",
        "SELECT POSITION('b' IN 'abc') FROM orders WHERE id = 1",
        "SELECT STRPOS('abc', 'b') FROM orders WHERE id = 1",
        "SELECT DATE_PART('year', CURRENT_DATE) FROM orders WHERE id = 1",
        "SELECT IF(1 = 1, 1, 0) FROM orders WHERE id = 1",
        'SELECT "SUM"(1) FROM orders WHERE id = 1',
        "SELECT CURRENT_USER FROM orders WHERE id = 1",
        "SELECT SESSION_USER FROM orders WHERE id = 1",
        "SELECT CURRENT_SCHEMA FROM orders WHERE id = 1",
        "SELECT CURRENT_CATALOG FROM orders WHERE id = 1",
        "SELECT CURRENT_TIME FROM orders WHERE id = 1",
        "SELECT LOCALTIME FROM orders WHERE id = 1",
        "SELECT 1 FROM orders WHERE id = 1 ORDER BY random()",
    ],
)
def test_check_sql_rejects_functions_outside_whitelist(sql: str) -> None:
    """Security checker should reject any function spelling absent from the whitelist."""
    schema = {"orders": {"columns": {"id": {}}}}

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["FUNCTION-001"]


def test_check_sql_rejects_qualified_whitelisted_function() -> None:
    """Function namespaces should not bypass the fixed function whitelist."""
    schema = {"orders": {"columns": {"id": {}}}}

    result = check_sql(
        "SELECT pg_catalog.COALESCE(NULL, 0) FROM orders WHERE id = 1",
        dialect="postgres",
        schema=schema,
    )

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["FUNCTION-001"]


def test_check_sql_allows_generate_series_joined_to_modeled_table() -> None:
    """The generate_series table function should remain subject to the semantic-table requirement."""
    schema = {"orders": {"columns": {"id": {}}}}
    sql = "SELECT orders.id FROM orders CROSS JOIN generate_series(1, 2) AS value WHERE orders.id = 1"

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False


def test_check_sql_allows_semantic_schema_table_and_column() -> None:
    """Security checker should allow a uniquely modeled source column."""
    schema = {"orders": {"columns": {"id": {"value_type": "integer"}}}}

    result = check_sql("SELECT id FROM orders WHERE id = 1", dialect="postgres", schema=schema)

    assert result.blocked is False


def test_check_sql_allows_modeled_pg_prefixed_table() -> None:
    """Relation prefixes should not override an explicit semantic whitelist entry."""
    schema = {"pg_business_metrics": {"columns": {"id": {}}}}

    result = check_sql(
        "SELECT id FROM pg_business_metrics WHERE id = 1",
        dialect="postgres",
        schema=schema,
    )

    assert result.blocked is False


def test_check_sql_allows_quoted_business_column_named_user() -> None:
    """Quoted source columns should remain governed by semantic metadata."""
    schema = {"orders": {"columns": {"id": {}, "user": {}}}}

    result = check_sql('SELECT "user" FROM orders WHERE id = 1', dialect="postgres", schema=schema)

    assert result.blocked is False


def test_check_sql_allows_dangerous_text_as_plain_data() -> None:
    """Dangerous names in string literals and aliases should not be treated as executable calls."""
    schema = {"orders": {"columns": {"id": {}}}}

    result = check_sql("SELECT 'pg_sleep' AS label FROM orders WHERE id = 1", dialect="postgres", schema=schema)

    assert result.blocked is False


def test_check_sql_allows_qualified_cte_column_named_current_time() -> None:
    """A qualified CTE column should not be confused with the CURRENT_TIME context function."""
    schema = {"orders": {"columns": {"id": {}, "time": {}}}}
    sql = (
        "WITH aligned_periods AS ("
        "SELECT time AS current_time FROM orders WHERE id = 1"
        ") SELECT aligned_periods.current_time FROM aligned_periods ORDER BY aligned_periods.current_time"
    )

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False


@pytest.mark.parametrize("qualifier", ["", "aligned_periods."])
def test_check_sql_resolves_cte_column_named_like_context_function(qualifier: str) -> None:
    """Qualified and unqualified CTE columns should use the same semantic resolution path."""
    schema = {"orders": {"columns": {"id": {}, "time": {}}}}
    sql = (
        "WITH aligned_periods AS ("
        "SELECT time AS current_time FROM orders WHERE id = 1"
        f") SELECT {qualifier}current_time FROM aligned_periods ORDER BY {qualifier}current_time"
    )

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False
    if not qualifier:
        assert result.normalized_sql is not None
        assert result.normalized_sql == sql.replace("current_time", '"current_time"')


def test_check_sql_resolves_base_column_named_like_context_function() -> None:
    """A modeled base column should disambiguate a bare context-function-shaped name."""
    schema = {"orders": {"columns": {"id": {}, "current_time": {}}}}

    result = check_sql("SELECT current_time FROM orders WHERE id = 1", dialect="postgres", schema=schema)

    assert result.blocked is False
    assert result.normalized_sql == 'SELECT "current_time" FROM orders WHERE id = 1'


def test_check_sql_semantic_normalization_preserves_other_source_syntax() -> None:
    """Semantic qualification should not rewrite unrelated allowed function or cast spellings."""
    schema = {"orders": {"columns": {"id": {}, "current_time": {}}}}
    sql = "SELECT current_time, NVL(NULL, 0), INSTR('abc', 'b'), '1'::INTEGER FROM orders WHERE id=1"

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False
    assert result.normalized_sql == (
        "SELECT \"current_time\", NVL(NULL, 0), INSTR('abc', 'b'), '1'::INTEGER FROM orders WHERE id=1"
    )


@pytest.mark.parametrize(
    "projection",
    [
        "time AS current_time",
        "SUM(id) AS current_time",
    ],
)
def test_check_sql_resolves_order_by_output_alias(projection: str) -> None:
    """ORDER BY should safely resolve a keyword-shaped output alias without changing its meaning."""
    schema = {"orders": {"columns": {"id": {}, "time": {}}}}
    sql = f"SELECT {projection} FROM orders WHERE id = 1 ORDER BY current_time"

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False
    assert result.normalized_sql is not None
    assert result.normalized_sql.endswith('ORDER BY "current_time"')


def test_check_sql_rejects_ambiguous_column_named_like_context_function() -> None:
    """A bare context-function-shaped name should fail closed when multiple sources expose it."""
    schema = {
        "orders": {"columns": {"id": {}, "current_time": {}}},
        "events": {"columns": {"id": {}, "current_time": {}}},
    }
    sql = "SELECT current_time FROM orders CROSS JOIN events WHERE orders.id = events.id"

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["FUNCTION-001"]


def test_check_sql_rejects_explicit_context_function_when_same_named_column_exists() -> None:
    """Explicit call syntax should preserve function intent even when a source exposes the same name."""
    schema = {"orders": {"columns": {"id": {}, "current_time": {}}}}

    result = check_sql("SELECT CURRENT_TIME() FROM orders WHERE id = 1", dialect="postgres", schema=schema)

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["FUNCTION-001"]


def test_check_sql_resolves_other_context_function_shaped_column_names() -> None:
    """Semantic disambiguation should apply to context-function names without per-name exceptions."""
    schema = {"orders": {"columns": {"id": {}, "role_name": {}}}}
    sql = (
        "WITH roles AS ("
        "SELECT role_name AS current_user FROM orders WHERE id = 1"
        ") SELECT current_user FROM roles ORDER BY current_user"
    )

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'No table like sys config' AS message",
        'SELECT "No table like sys config" AS message',
        "SELECT 1",
        "SELECT NOW()",
        "SELECT generate_series(1, 2)",
    ],
)
def test_check_sql_rejects_query_without_semantic_table(sql: str) -> None:
    """NL2SQL candidates should reference at least one modeled business table."""
    schema = {"orders": {"columns": {"id": {}}}}

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["SCHEMA-003"]
    assert "modeled business table" in result.violations[0].message
    assert "exposed columns" in result.violations[0].message


def test_check_sql_rejects_table_missing_from_semantic_schema() -> None:
    """Security checker should reject an unmodeled source relation."""
    result = check_sql("SELECT id FROM pg_authid WHERE id = 1", dialect="postgres", schema={})

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["SCHEMA-001"]
    assert "provided semantic schema" in result.violations[0].message
    assert "table qualifier or alias" in result.violations[0].message


def test_check_sql_checks_base_table_shadowed_by_cte_name() -> None:
    """A CTE alias should not exempt a same-named base table from the whitelist."""
    sql = "WITH secret AS (SELECT id FROM secret WHERE id = 1) SELECT id FROM secret"

    result = check_sql(sql, dialect="postgres", schema={})

    assert result.blocked is True
    assert "SCHEMA-001" in [violation.rule_id for violation in result.violations]


def test_check_sql_rejects_column_missing_from_semantic_schema() -> None:
    """Security checker should reject an unmodeled source column."""
    schema = {"orders": {"columns": {"id": {"value_type": "integer"}}}}

    result = check_sql("SELECT password FROM orders WHERE id = 1", dialect="postgres", schema=schema)

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["SCHEMA-002"]
    assert "exists under the referenced table" in result.violations[0].message
    assert "table or CTE alias" in result.violations[0].message


def test_check_sql_explains_how_to_fix_ambiguous_source_column() -> None:
    """A column-resolution issue should tell Reflector how to repair the source reference."""
    schema = {
        "orders": {"columns": {"id": {}}},
        "customers": {"columns": {"id": {}}},
    }

    result = check_sql(
        "SELECT id FROM orders CROSS JOIN customers WHERE orders.id = customers.id",
        dialect="postgres",
        schema=schema,
    )

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["SCHEMA-002"]
    assert "qualify the column with its unique source" in result.violations[0].message


def test_check_sql_allows_unqualified_unique_semantic_table() -> None:
    """An unqualified relation should resolve when its base name is unique in metadata."""
    schema = {"business.orders": {"columns": {"id": {}}}}

    result = check_sql("SELECT id FROM orders WHERE id = 1", dialect="postgres", schema=schema)

    assert result.blocked is False


def test_check_sql_allows_qualified_semantic_table() -> None:
    """A qualified relation should match the same qualified semantic object."""
    schema = {"business.orders": {"columns": {"id": {}}}}

    result = check_sql("SELECT id FROM business.orders WHERE id = 1", dialect="postgres", schema=schema)

    assert result.blocked is False


def test_check_sql_rejects_ambiguous_unqualified_semantic_table() -> None:
    """An unqualified relation should fail closed when multiple semantic objects share its name."""
    schema = {
        "business.orders": {"columns": {"id": {}}},
        "archive.orders": {"columns": {"id": {}}},
    }

    result = check_sql("SELECT id FROM orders WHERE id = 1", dialect="postgres", schema=schema)

    assert result.blocked is True
    assert "SCHEMA-001" in [violation.rule_id for violation in result.violations]


def test_check_sql_allows_cross_join_with_modeled_sources() -> None:
    """Security checker should explicitly allow CROSS JOIN syntax."""
    schema = {
        "orders": {"columns": {"id": {"value_type": "integer"}}},
        "customers": {"columns": {"id": {"value_type": "integer"}}},
    }

    result = check_sql(
        "SELECT orders.id FROM orders CROSS JOIN customers WHERE orders.id = 1",
        dialect="postgres",
        schema=schema,
    )

    assert result.blocked is False


def test_check_sql_allows_comma_separated_cross_join() -> None:
    """Security checker should allow comma-separated sources as an implicit CROSS JOIN."""
    schema = {
        "orders": {"columns": {"id": {"value_type": "integer"}}},
        "customers": {"columns": {"id": {"value_type": "integer"}}},
    }

    result = check_sql(
        "SELECT orders.id FROM orders, customers WHERE orders.id = 1",
        dialect="postgres",
        schema=schema,
    )

    assert result.blocked is False


@pytest.mark.parametrize(
    "join_sql",
    [
        "JOIN customers ON orders.customer_id = customers.id",
        "INNER JOIN customers ON orders.customer_id = customers.id",
        "LEFT JOIN customers ON orders.customer_id = customers.id",
        "LEFT OUTER JOIN customers ON orders.customer_id = customers.id",
        "RIGHT JOIN customers ON orders.customer_id = customers.id",
        "RIGHT OUTER JOIN customers ON orders.customer_id = customers.id",
        "FULL JOIN customers ON orders.customer_id = customers.id",
        "FULL OUTER JOIN customers ON orders.customer_id = customers.id",
        "JOIN customers USING (id)",
        "CROSS JOIN customers",
    ],
)
def test_check_sql_allows_whitelisted_join_types(join_sql: str) -> None:
    """Security checker should allow ordinary, outer, and explicit cross joins."""
    schema = {
        "orders": {"columns": {"id": {}, "customer_id": {}}},
        "customers": {"columns": {"id": {}}},
    }
    sql = f"SELECT orders.id FROM orders {join_sql} WHERE orders.id = 1"

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False


def test_check_sql_allows_whitelisted_query_clauses() -> None:
    """Security checker should allow the fixed filtering, grouping, sorting, and filter clauses."""
    schema = {"orders": {"columns": {"customer_id": {}, "amount": {}, "status": {}}}}
    sql = (
        "SELECT DISTINCT customer_id, SUM(amount) FILTER (WHERE amount > 0) AS total "
        "FROM orders WHERE status IN ('paid') GROUP BY customer_id "
        "HAVING SUM(amount) > 0 ORDER BY total LIMIT 10"
    )

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False


@pytest.mark.parametrize("condition", ["amount IS NULL", "amount IS NOT NULL"])
def test_check_sql_allows_whitelisted_null_predicates(condition: str) -> None:
    """Security checker should allow only the documented null predicates."""
    schema = {"orders": {"columns": {"id": {}, "amount": {}}}}

    result = check_sql(f"SELECT id FROM orders WHERE {condition}", dialect="postgres", schema=schema)

    assert result.blocked is False


@pytest.mark.parametrize(
    "condition",
    [
        "name LIKE 'A%'",
        "name LIKE '%term%'",
        "name NOT LIKE '_temp%'",
        "name LIKE '*'",
        "name LIKE 'mobile_game%'",
        "name LIKE '%game%'",
    ],
)
def test_check_sql_allows_like_with_literal_search_text(condition: str) -> None:
    """LIKE patterns should contain literal search text in addition to optional wildcards."""
    schema = {"orders": {"columns": {"id": {}, "name": {}}}}

    result = check_sql(f"SELECT id FROM orders WHERE {condition}", dialect="postgres", schema=schema)

    assert result.blocked is False


@pytest.mark.parametrize(
    "condition",
    [
        "name LIKE '%'",
        "name LIKE '_'",
        "name LIKE '%%'",
        "name LIKE '%_%'",
        "name LIKE pattern",
    ],
)
def test_check_sql_rejects_like_without_literal_search_text(condition: str) -> None:
    """LIKE should reject wildcard-only and non-literal patterns."""
    schema = {"orders": {"columns": {"id": {}, "name": {}, "pattern": {}}}}

    result = check_sql(f"SELECT id FROM orders WHERE {condition}", dialect="postgres", schema=schema)

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["SYNTAX-001"]


@pytest.mark.parametrize("operator", ["UNION", "UNION ALL"])
def test_check_sql_allows_union_with_filtered_business_branches(operator: str) -> None:
    """UNION variants should compose independently filtered read-only SELECT branches."""
    schema = {"orders": {"columns": {"id": {}}}}
    sql = f"SELECT id FROM orders WHERE id = 1 {operator} SELECT id FROM orders WHERE id = 2 ORDER BY id"

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False


def test_check_sql_allows_union_all_literal_cte_joined_to_business_table() -> None:
    """Literal UNION ALL CTEs should be allowed when the complete query reads a modeled business table."""
    schema = {"orders": {"columns": {"id": {}, "status": {}}}}
    sql = (
        "WITH statuses AS (SELECT 'paid' AS status UNION ALL SELECT 'pending') "
        "SELECT orders.id FROM orders JOIN statuses ON orders.status = statuses.status WHERE orders.id = 1"
    )

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False


def test_check_sql_allows_union_all_single_row_aggregate_branches() -> None:
    """UNION ALL should preserve the full-table exemption for single-row aggregate branches."""
    schema = {"orders": {"columns": {"id": {}}}}
    sql = "SELECT COUNT(*) FROM orders UNION ALL SELECT COUNT(*) FROM orders"

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False


def test_check_sql_rejects_union_all_with_unfiltered_business_branch() -> None:
    """Every UNION ALL branch that reads business rows should retain a filter or another resource exemption."""
    schema = {"orders": {"columns": {"id": {}}}}
    sql = "SELECT id FROM orders WHERE id = 1 UNION ALL SELECT id FROM orders"

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["RESOURCE-009"]


def test_check_sql_does_not_mask_unfiltered_union_branch_with_tablesample() -> None:
    """A TABLESAMPLE on one UNION branch must not mask an unfiltered read in another."""
    schema = {"huge": {"columns": {"id": {}}}, "t": {"columns": {"id": {}}}}
    sql = "SELECT * FROM huge UNION SELECT id FROM t TABLESAMPLE SYSTEM (1)"

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["RESOURCE-009"]


@pytest.mark.parametrize("operator", ["UNION", "UNION ALL"])
@pytest.mark.parametrize(
    ("right_projection", "rule_id"),
    [
        ("MD5(status)", "FUNCTION-001"),
        ("password", "SCHEMA-002"),
    ],
)
def test_check_sql_recursively_checks_every_union_branch(
    operator: str,
    right_projection: str,
    rule_id: str,
) -> None:
    """A safe UNION branch should not hide a violation in another branch."""
    schema = {"orders": {"columns": {"id": {}, "status": {}}}}
    sql = f"SELECT id FROM orders WHERE id = 1 {operator} SELECT {right_projection} FROM orders WHERE id = 2"

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is True
    assert rule_id in [violation.rule_id for violation in result.violations]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 EXCEPT SELECT 2",
        "SELECT 1 INTERSECT SELECT 2",
        "SELECT amount IS TRUE FROM orders WHERE id = 1",
        "SELECT id FROM orders WHERE name ILIKE 'A%'",
        "SELECT id FROM orders WHERE name LIKE 'A!_%' ESCAPE '!'",
        "SELECT id FROM orders WHERE amount BETWEEN 1 AND 10",
        "SELECT SUM(amount) OVER (PARTITION BY id) FROM orders WHERE id = 1",
        "SELECT id FROM orders WHERE id > 0 LIMIT 10 OFFSET 1",
        "SELECT id FROM orders FETCH FIRST 10 ROWS ONLY",
        "SELECT id FROM orders TABLESAMPLE SYSTEM (10)",
        "SELECT ARRAY[1, 2]",
        "SELECT * FROM (VALUES (1)) AS source(id)",
        "WITH recent AS MATERIALIZED (SELECT id FROM orders WHERE id = 1) SELECT id FROM recent",
        "SELECT orders.id FROM orders CROSS JOIN LATERAL (SELECT orders.id) AS item WHERE orders.id = 1",
        "SELECT orders.id FROM orders CROSS JOIN customers ON orders.customer_id = customers.id WHERE orders.id = 1",
        "SELECT orders.id FROM orders SEMI JOIN customers ON orders.customer_id = customers.id WHERE orders.id = 1",
        "SELECT orders.id FROM orders LEFT INNER JOIN customers "
        "ON orders.customer_id = customers.id WHERE orders.id = 1",
        "SELECT orders.id FROM orders RIGHT CROSS JOIN customers WHERE orders.id = 1",
        "SELECT a.id FROM a NATURAL JOIN b WHERE a.id = 1",
        "SELECT DISTINCT ON (customer_id) customer_id FROM orders WHERE customer_id = 1",
        "SELECT customer_id, SUM(amount) FROM orders WHERE customer_id = 1 GROUP BY ROLLUP (customer_id)",
        "WITH RECURSIVE x AS (SELECT 1) SELECT * FROM x",
    ],
)
def test_check_sql_rejects_query_syntax_outside_whitelist(sql: str) -> None:
    """Security checker should reject parsed query syntax absent from the whitelist."""
    schema = {
        "orders": {"columns": {"id": {}, "customer_id": {}, "amount": {}, "name": {}}},
        "customers": {"columns": {"id": {}}},
    }

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["SYNTAX-001"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_catalog.coalesce(1, 0)",
        "SELECT gs_test(1)",
        "SELECT pgxc_test(1)",
        "SELECT dbms_sql.open_cursor()",
        "SELECT dbms_job.submit(1)",
        "SELECT has_table_privilege('orders', 'select')",
        "SELECT query_to_xml('select 1', true, false, '')",
        "SELECT current_role",
        "SELECT user",
        "SELECT dbe_perf.some_metric()",
        "SELECT snapshot.some_metric()",
    ],
)
def test_check_sql_rejects_non_whitelisted_system_function_families(sql: str) -> None:
    """Security checker should reject system function families absent from the whitelist."""
    result = check_sql(sql, dialect="postgres", schema={})

    assert result.blocked is True
    assert "FUNCTION-001" in [violation.rule_id for violation in result.violations]


@pytest.mark.parametrize(
    ("sql", "rule_id"),
    [
        ("SELECT lpad('x', unknown_size, 'y')", "RESOURCE-006"),
        ("SELECT rpad('x', unknown_size, 'y')", "RESOURCE-006"),
    ],
)
def test_check_sql_rejects_unbounded_resource_functions(sql: str, rule_id: str) -> None:
    """Security checker should reject provably huge or unknown resource functions."""
    result = check_sql(sql, dialect="postgres", schema={})

    assert result.blocked is True
    assert rule_id in [violation.rule_id for violation in result.violations]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT lpad('x', 4194304, 'y') FROM orders WHERE id = 1",
        "SELECT rpad('x', 4194304, 'y') FROM orders WHERE id = 1",
    ],
)
def test_check_sql_allows_resource_function_thresholds(sql: str) -> None:
    """Resource functions should remain allowed at their documented inclusive thresholds."""
    schema = {"orders": {"columns": {"id": {}}}}

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False


def test_check_sql_rejects_ast_over_node_limit() -> None:
    """Security checker should reject a parsed tree above the fixed AST-node limit."""
    sql = "SELECT " + ",".join("1" for _ in range(10_001))

    result = check_sql(sql, dialect="postgres", schema={})

    assert result.blocked is True
    assert "RESOURCE-002" in [violation.rule_id for violation in result.violations]


def test_check_sql_rejects_sql_text_over_byte_limit() -> None:
    """Security checker should count the SQL-text limit in UTF-8 bytes."""
    sql = "SELECT '" + ("测" * 350_000) + "'"

    result = check_sql(sql, dialect="postgres", schema={})

    assert result.blocked is True
    assert [violation.rule_id for violation in result.violations] == ["RESOURCE-001"]


@pytest.mark.parametrize(
    ("sql", "rule_id"),
    [
        ("SELECT a.id FROM a JOIN b WHERE a.id = 1", "RESOURCE-007"),
        ("SELECT a.id FROM a JOIN b ON 1 = 1 WHERE a.id = 1", "RESOURCE-007"),
        ("SELECT id FROM orders WHERE id = 1 OR TRUE", "RESOURCE-008"),
        ("SELECT id FROM orders", "RESOURCE-009"),
    ],
)
def test_check_sql_rejects_high_confidence_query_shapes(sql: str, rule_id: str) -> None:
    """Security checker should reject high-confidence resource abuse shapes."""
    schema = {
        "a": {"columns": {"id": {}}},
        "b": {"columns": {"id": {}}},
        "orders": {"columns": {"id": {}}},
    }

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is True
    assert rule_id in [violation.rule_id for violation in result.violations]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM orders",
        "SELECT id FROM orders WHERE id = 1",
        "SELECT id FROM orders LIMIT 100",
        "SELECT a.id FROM a CROSS JOIN b WHERE a.id = 1",
        "SELECT id FROM orders WHERE id = 1 AND TRUE",
    ],
)
def test_check_sql_allows_documented_query_shapes(sql: str) -> None:
    """Security checker should allow query shapes that the workbook guarantees."""
    schema = {
        "a": {"columns": {"id": {}}},
        "b": {"columns": {"id": {}}},
        "orders": {"columns": {"id": {}}},
    }

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM orders UNION ALL SELECT id FROM orders",
        "WITH recent AS (SELECT id FROM orders) SELECT id FROM recent",
    ],
)
def test_check_sql_rejects_unfiltered_base_reads_inside_query_tree(sql: str) -> None:
    """Unfiltered row-return queries should reject direct or CTE-backed full output."""
    schema = {"orders": {"columns": {"id": {}}}}

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is True
    assert "RESOURCE-009" in [violation.rule_id for violation in result.violations]


@pytest.mark.parametrize(
    "sql",
    [
        "WITH recent AS (SELECT id FROM orders WHERE id = 1) SELECT id FROM recent",
        "SELECT id FROM (SELECT id FROM orders WHERE id = 1) AS recent",
        "WITH recent AS (SELECT id FROM orders) SELECT id FROM recent WHERE id = 1",
        "SELECT id FROM (SELECT id FROM orders) AS recent LIMIT 10",
    ],
)
def test_check_sql_allows_filtered_base_reads_inside_query_tree(sql: str) -> None:
    """Outer CTE and subquery projections should not be mistaken for base-table scans."""
    schema = {"orders": {"columns": {"id": {}}}}

    result = check_sql(sql, dialect="postgres", schema=schema)

    assert result.blocked is False


def test_flat_and_chain_is_resource_008() -> None:
    cond = " AND ".join(["1=1"] * 1200)
    result = check_sql(
        f"SELECT id FROM orders WHERE {cond}",
        dialect="postgres",
        schema={"orders": {"columns": {"id": {}}}},
    )
    assert result.blocked is True
    assert "RESOURCE-008" in [item.rule_id for item in result.violations]


def test_deeply_nested_boolean_fails_closed() -> None:
    cond = "(" * 2000 + "1=1" + ")" * 2000
    result = check_sql(
        f"SELECT id FROM orders WHERE {cond}",
        dialect="postgres",
        schema={"orders": {"columns": {"id": {}}}},
    )
    assert result.blocked is True
