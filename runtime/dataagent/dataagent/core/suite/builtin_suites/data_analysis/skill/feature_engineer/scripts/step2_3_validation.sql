-- step2_3: 聚合宽表用户键门禁（单条 SELECT）
-- 本文件必须在 step2_3_wide_complete CREATE 完成后独立提交。

SELECT
    count() AS n_rows,
    uniqExact(<user_id>) AS n_user_id,
    count() = uniqExact(<user_id>) AS user_key_unique,
    (
        SELECT count()
        FROM {{output_database}}.step2_2_wide_cleaned
    ) AS expected_rows,
    n_rows = expected_rows AS row_count_unchanged
FROM {{output_database}}.step2_3_wide_complete;

-- step2_3 字符串高基数门禁（独立提交，单条 SELECT）
-- 检查 step2_3_wide_complete 中是否残留未转换的 String / Nullable(String)。
-- LowCardinality(String) 是允许的落点（版本等非数值分类）。城市原列不行：必须只留 *_tier。
-- 如果返回任何行，门禁失败，阻塞不通过。
--
-- ⛔ 展开规范：agent 必须在 submit 此文件前，从 step2_0_source_data_analyze.md 的
--    ## 列表字段检测结果 表格中提取 list_delimiter != null 的全部字段名，
--    替换下面的 /*__LIST_FIELD_NAMES__*/ 占位符。禁止提交含 PLACEHOLDER 的版本。
--
-- 提取命令：
--   rg '\| (.+?) \| .+? \| "(#|\^)" \|' step2_0_source_data_analyze.md \
--     | sed 's/|.*//' | tr -d ' ' | sed "s/^/'/" | sed "s/$/',/" | tr '\n' ' ' | sed 's/, $//'

SELECT
    name AS column_name,
    type,
    CASE
        WHEN name IN (/*__LIST_FIELD_NAMES__*/)
        THEN 'CRITICAL: LIST FIELD NOT SPLIT — splitByChar missing. Original field leaked to CSV'
        WHEN type IN ('String', 'Nullable(String)')
        THEN 'String survived step2_3 — sample then cast: numeric→Float64, comma-list→length+is_empty, else LowCardinality(String)'
        ELSE 'Non-LowCardinality string type survived step2_3'
    END AS diagnosis
FROM system.columns
WHERE database = '{{output_database}}'
  AND table = 'step2_3_wide_complete'
  AND (
        type IN ('String', 'Nullable(String)')
        OR (
            position(type, 'String') > 0
            AND position(type, 'LowCardinality') = 0
        )
      )
  AND name NOT IN ('<user_id>', '<label>', '<age>', '<gender>')
FORMAT TSVWithNames;

-- step2_3 城市原列门禁（独立提交，单条 SELECT）
-- 地名会过拟合。step2_3_wide_complete / 最终 CSV 只允许 {col}_tier，
-- 禁止残留 city / city_name / *城市* 原列（即使已是 LowCardinality(String)）。
-- 如果返回任何行，门禁失败，阻塞不通过。

SELECT
    name AS column_name,
    type,
    'CRITICAL: raw city column leaked — overfitting. EXCEPT the original column and keep only {col}_tier' AS diagnosis
FROM system.columns
WHERE database = '{{output_database}}'
  AND table = 'step2_3_wide_complete'
  AND name NOT LIKE '%_tier'
  AND NOT match(name, '(?i)cityhash|hash_city')
  AND (
        match(name, '(?i)(^|_)(city|city_name|reside_city|user_city)(_|$)')
        OR position(name, '城市') > 0
      )
FORMAT TSVWithNames;
