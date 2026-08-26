-- step2_2: 清洗决策画像（基于 step2_0 已算好的列画像，不重复计算）
-- 输入: {{output_database}}.step2_0_column_profile（step2_0 阶段2 已产出）
-- 输出: {{output_database}}.step2_2_cleaning_report
-- 逻辑: step2_1 合表仅加来源表前缀，missing_rate/n_unique 不变，
--        因此直接引用 step2_0 的预计算结果做清洗决策，不再对 step2_1 重跑聚合。
-- 本文件整体必须作为一条独立 ClickHouse MCP command 提交。禁止拆为多条。
--
-- ⛔ missing_rate 存储格式（重要）：step2_0_column_profile.missing_rate 存储为百分比数值
--   （0-100 范围，如 0.69 表示 0.69%、65.0 表示 65%），不是 0-1 比例。
--   因此 DROP 阈值为 > 50（表示 > 50%），不是 > 0.5。
--   反面案例 061349：agent 用 > 0.5 判断，导致 career (0.69% → 0.69 > 0.5 TRUE)、
--   city (9.15% → 9.15 > 0.5 TRUE) 等 28 个低缺失率字段被误删。

CREATE OR REPLACE TABLE {{output_database}}.step2_2_cleaning_report
ENGINE = MergeTree
ORDER BY feature
AS
SELECT
    column_name AS feature,
    data_type,
    missing_rate AS null_rate,
    n_unique,
    sample_values,
    column_name IN ('<user_id>', '<label>', '<age>', '<gender>') AS is_protect,
    (position(sample_values, '#') > 0 OR position(sample_values, '^') > 0) AS is_list_field,
    CASE
        -- 保护列：强制保留
        WHEN column_name IN ('<user_id>', '<label>', '<age>', '<gender>') THEN 'KEEP'
        -- 列表字段：永不 DROP。高 null_rate 含义是"用户没有该列表"（空列表），
        --  这是有效信号——拆分后所有二元特征值应为 0，而不是删除整列。
        -- 反面案例：game_interest_theme_u (null_rate=65%)、social_interest_strange_social_app_u (95.9%)
        --  被误 DROP，导致 step2_3 无法对其执行 splitByChar 展开二元特征。
        WHEN position(sample_values, '#') > 0 OR position(sample_values, '^') > 0 THEN 'KEEP'
        -- 高缺失率普通字段：删除（missing_rate 为百分比值，> 50 表示 > 50%）
        WHEN missing_rate > 50 THEN 'DROP'
        -- 常量字段：删除
        WHEN n_unique <= 1 THEN 'DROP'
        -- 其余：保留
        ELSE 'KEEP'
    END AS recommendation
FROM {{output_database}}.step2_0_column_profile
ORDER BY feature;
