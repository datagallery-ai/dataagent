"""Verify that the deagent DDL renderer retains source metadata semantics."""

from dataagent.core.suite.builtin_suites.de_agent.tools.schema_ddl import schema_to_ddl


def test_original_types_and_qualified_identifiers():
    """Keep precise and nested types while quoting each table identifier component."""
    columns = {
        "amount": {"value_type": "DECIMAL(38, 12)"},
        "events": {"value_type": "ARRAY<STRUCT<ts:TIMESTAMP,amount:DECIMAL(20,5)>>"},
        "day": {"value_type": "DATE"},
        "a`b.c": {"value_type": "VARCHAR(128)"},
    }
    rendered = schema_to_ddl({"db.table": {"columns": columns}})
    assert "CREATE TABLE `db`.`table` (" in rendered
    assert "`amount` DECIMAL(38, 12)," in rendered
    assert "`events` ARRAY<STRUCT<ts:TIMESTAMP,amount:DECIMAL(20,5)>>," in rendered
    assert "`day` DATE," in rendered
    assert "`a``b.c` VARCHAR(128)\n);" in rendered


def test_complete_join_metadata_with_missing_schema_endpoints():
    """Retain Join type, expression, evidence and additional structured fields."""
    relation = {
        "src": "db.left.id",
        "target": "db.right.id",
        "rel_type": "LEFT JOIN",
        "expression": "db.left.id = db.right.id AND db.right.active = 1",
        "evidence": "业务关联\n不保证唯一",
        "cardinality": {"left": "many", "right": "many"},
    }
    rendered = schema_to_ddl({}, [relation])
    assert '-- src: "db.left.id"' in rendered
    assert '-- target: "db.right.id"' in rendered
    assert '-- rel_type: "LEFT JOIN"' in rendered
    assert '-- expression: "db.left.id = db.right.id AND db.right.active = 1"' in rendered
    assert '-- evidence: "业务关联\\n不保证唯一"' in rendered
    assert '-- cardinality: {"left": "many", "right": "many"}' in rendered
    assert "FOREIGN KEY" not in rendered


def test_join_metadata_does_not_imply_foreign_keys():
    """Keep business Join evidence without inferring foreign-key or uniqueness constraints."""
    schema = {
        "db.left": {"columns": {"id": {"value_type": "BIGINT"}}},
        "db.right": {"columns": {"id": {"value_type": "BIGINT"}}},
    }
    relation = {
        "src": "db.left.id",
        "target": "db.right.id",
        "rel_type": "LEFT JOIN",
        "expression": "db.left.id = db.right.id AND db.right.active = 1",
        "evidence": "关联证据",
    }
    rendered = schema_to_ddl(schema, [relation, relation])
    assert "FOREIGN KEY" not in rendered
    assert "`id` BIGINT\n);" in rendered
    assert '-- rel_type: "LEFT JOIN"' in rendered
    assert '-- expression: "db.left.id = db.right.id AND db.right.active = 1"' in rendered
    assert '-- evidence: "关联证据"' in rendered


def test_non_null_examples_include_false_and_zero():
    """Preserve meaningful falsey examples and render non-null examples as JSON comments."""
    columns = {
        "status": {"value_type": "STRING", "example_values": ["安装", "卸载"]},
        "count": {"value_type": "INT", "example_values": 0},
        "enabled": {"value_type": "BOOLEAN", "example_values": False},
    }
    rendered = schema_to_ddl({"db.table": {"columns": columns}})
    assert '-- example_values: ["安装", "卸载"]' in rendered
    assert "-- example_values: 0" in rendered
    assert "-- example_values: false" in rendered


def test_nullable_descriptions_multiline_comments_and_missing_type():
    """Keep multiline comments and mark the Hive/Spark missing-type placeholder explicitly."""
    schema = {
        "db.table": {
            "description": "表说明\n第二行",
            "columns": {
                "id": {"value_type": None, "description": None},
                "name": {"value_type": "STRING", "description": "字段说明\n第二行", "example_values": []},
            },
        }
    }
    rendered = schema_to_ddl(schema)
    assert "-- 表说明\n-- 第二行" in rendered
    assert "    -- 字段说明\n    -- 第二行" in rendered
    assert "    -- example_values: []" in rendered
    assert "Original field type unavailable; STRING is a Hive/Spark syntax placeholder only." in rendered
    assert "Retrieve the actual type before choosing casts or calculations based on this field." in rendered
    assert "`id` STRING," in rendered


def test_unavailable_columns_are_not_rendered_as_empty_table_definitions():
    """Distinguish unavailable column metadata from a table definition with no columns."""
    for table_info in ({"description": "表说明", "columns": {}}, {"description": "表说明"}):
        rendered = schema_to_ddl({"db.table": table_info})
        assert "-- 表说明" in rendered
        assert "-- Table `db`.`table`: column metadata unavailable." in rendered
        assert "Retrieve column metadata before referencing this table's columns; do not invent columns." in rendered
        assert "CREATE TABLE" not in rendered
