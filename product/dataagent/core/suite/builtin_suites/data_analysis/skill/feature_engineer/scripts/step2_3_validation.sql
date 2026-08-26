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
-- 检查 step2_3_wide_complete 中是否残留未处理的 String 高基数列。
-- 如果返回任何行，说明有高基数字符串字段未被分箱/分割，门禁失败，阻塞不通过。
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
        ELSE 'String column survived step2_3 — binning/splitting required'
    END AS diagnosis
FROM system.columns
WHERE database = '{{output_database}}'
  AND table = 'step2_3_wide_complete'
  AND type LIKE 'String%'
  AND name NOT IN ('<user_id>', '<label>', '<age>', '<gender>')
FORMAT TSVWithNames;
