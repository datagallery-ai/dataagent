-- step2_0: 标签、用户键与源表覆盖门禁（单条 SELECT）
-- 本文件必须在两个画像 CREATE 完成后作为一条独立 ClickHouse MCP command 提交。
-- collect_job 的结果必须写回 schema_resolution.json 的 key_validation 字段（含 label 验证结果）。

SELECT
    count() AS n_rows,
    uniqExact(<user_id>) AS n_users,
    countIf(<label> IS NULL OR toString(<label>) = '') AS n_label_missing,
    countIf(toString(<label>) NOT IN ('0', '1')) AS n_label_invalid,
    countIf(toString(<label>) = '0') AS n_negative,
    countIf(toString(<label>) = '1') AS n_positive,
    (
        SELECT count()
        FROM {{output_database}}.step2_0_table_profile
    ) AS profiled_sources,
    /*__EXPECTED_SOURCE_TABLE_COUNT__*/ AS expected_sources,
    profiled_sources = expected_sources AS source_coverage_ok
FROM {{output_database}}.<user_table>;
