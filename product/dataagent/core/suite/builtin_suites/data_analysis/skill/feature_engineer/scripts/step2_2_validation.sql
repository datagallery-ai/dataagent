-- step2_2: 清洗结果完整性门禁（单条 SELECT）
-- 本文件必须在 cleaning_report 与 wide_cleaned 两个 CREATE 完成后独立提交。

SELECT
    count() AS n_rows,
    uniqExact(<user_id>) AS n_user_id,
    count() = uniqExact(<user_id>) AS user_key_unique,
    (
        SELECT count()
        FROM {{output_database}}.step2_2_cleaning_report
    ) AS profiled_columns,
    (
        SELECT count()
        FROM system.columns
        WHERE database = '{{output_database}}'
          AND table = 'step2_1_wide_simple'
    ) AS expected_columns,
    profiled_columns = expected_columns AS column_coverage_ok
FROM {{output_database}}.step2_2_wide_cleaned;

-- step2_2 清洗决策正确性门禁（独立提交，单条 SELECT）
-- ⛔ 高缺失率字段残留检测：查出 cleaning_report 中 recommendation='KEEP' 但 null_rate > 50 的字段。
-- null_rate 存储为百分比数值（0-100 范围，50 = 50%），不是 0-1 比例。
-- cleaning_report 的数据来自 step2_0_column_profile（预计算结果），不存在"抽样误差"问题。
-- 如果返回任何行，说明 cleaning_report.sql 中的 CASE WHEN 被绕过（直接写了字符串字面量 'KEEP'）。
-- 必须回退修正 cleaning_report.sql，重新建表。

SELECT
    feature AS column_name,
    null_rate,
    recommendation,
    'ERROR: null_rate > 50 but KEEP — CASE WHEN bypassed, likely hard-coded string literal' AS diagnosis
FROM {{output_database}}.step2_2_cleaning_report
WHERE recommendation = 'KEEP'
  AND null_rate > 50
FORMAT TSVWithNames;
