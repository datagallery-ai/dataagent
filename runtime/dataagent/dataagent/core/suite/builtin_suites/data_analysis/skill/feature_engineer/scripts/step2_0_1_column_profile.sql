-- step2_0_1 column profile：由 step2_0_1_expand_sql.py 展开（表名列名运行时取自 schema_resolution.json / step1_output_meta.json）

CREATE OR REPLACE TABLE {{output_database}}.step2_0_column_profile
ENGINE = MergeTree
ORDER BY (table_name, column_name)
AS
/*__SOURCE_COLUMN_PROFILE_SELECTS__*/;
