-- step2_3: 清洗决策报告（直接画像聚合宽表，用户粒度）
-- 输入: {{output_database}}.step2_2_wide_aggregated
-- 输出: {{output_database}}.step2_3_cleaning_report
-- 本文件整体作为一条独立 `<warehouse>` MCP command 提交。
--
-- 决策作用在聚合宽表上，粒度是用户，不是源表行。
-- 画像动态块由 step2_3_expand_sql.py 按宽表列清单展开（每列一条 SELECT，UNION ALL）。
-- 列表字段已在 step2_2 拆成二元特征、字符串已 CAST，本步无列表/字符串特殊分支。
-- null_rate 为百分比数值（0-100，如 0.69 表示 0.69%）。DROP 阈值与保护列由
-- step2_3_expand_sql.py 从 references/cleaning_policy.json 注入，禁止在本文件写死数值。

CREATE OR REPLACE TABLE {{output_database}}.step2_3_cleaning_report
ENGINE = MergeTree
ORDER BY feature
AS
WITH profile AS (
/*__WIDE_COLUMN_PROFILE_SELECTS__*/
)
SELECT
    column_name AS feature,
    data_type,
    null_rate,
    n_unique,
    sample_values,
    column_name IN (/*__PROTECTED_COLUMNS__*/) AS is_protect,
    CASE
        -- 保护列：强制保留
        WHEN column_name IN (/*__PROTECTED_COLUMNS__*/) THEN 'KEEP'
        -- 高缺失率普通字段：删除（用户粒度缺失率）
        WHEN null_rate > /*__NULL_RATE_DROP_THRESHOLD__*/ THEN 'DROP'
        -- 常量字段：删除
        WHEN n_unique <= /*__CONSTANT_MAX_UNIQUE__*/ THEN 'DROP'
        -- 其余：保留
        ELSE 'KEEP'
    END AS recommendation
FROM profile
ORDER BY feature;
