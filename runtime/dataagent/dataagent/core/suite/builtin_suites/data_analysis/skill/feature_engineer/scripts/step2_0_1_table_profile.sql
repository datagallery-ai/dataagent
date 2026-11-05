-- step2_0_1 table profile：由 step2_0_1_expand_sql.py 展开（表名列名运行时取自 schema_resolution.json / step1_output_meta.json）

CREATE OR REPLACE TABLE {{output_database}}.step2_0_table_profile
ENGINE = MergeTree
ORDER BY table_name
AS
/*__SOURCE_TABLE_PROFILE_SELECTS__*/;
