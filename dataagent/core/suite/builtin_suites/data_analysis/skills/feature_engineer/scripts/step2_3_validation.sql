-- step2_3: 清洗结果完整性门禁（单条 SELECT）
-- 本文件必须在 cleaning_report 与 wide_cleaned 两个 CREATE 完成后独立提交。

SELECT
    count() AS n_rows,
    uniqExact(<user_id>) AS n_user_id,
    count() = uniqExact(<user_id>) AS user_key_unique,
    (
        SELECT count()
        FROM {{database}}.step2_3_cleaning_report
    ) AS profiled_columns,
    (
        SELECT count()
        FROM system.columns
        WHERE database = '{{database}}'
          AND table = 'step2_2_wide_simple'
    ) AS expected_columns,
    profiled_columns = expected_columns AS column_coverage_ok
FROM {{database}}.step2_3_wide_cleaned;
