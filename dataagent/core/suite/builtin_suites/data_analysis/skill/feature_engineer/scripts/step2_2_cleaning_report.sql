-- step2_2: 合表后清洗决策画像（ClickHouse 单语句动态模板）
-- 注意：本画像针对 step2_1 合表后的宽表，与 step2_0 原始源表画像目的不同。
--   step2_0 → 探索性画像（理解原始数据，产出分类、常量候选、聚合建议）
--   step2_2 → 决策性画像（合表后字段，产出 KEEP/DROP 清洗建议）
-- 输入: {{output_database}}.step2_1_wide_simple 的实际列清单
-- 输出: {{output_database}}.step2_2_cleaning_report
-- 每个实际字段生成一个 SELECT，以 UNION ALL 连接；recommendation 只允许 KEEP 或 DROP。
-- 本文件整体必须作为一条独立 ClickHouse MCP command 提交。

CREATE OR REPLACE TABLE {{output_database}}.step2_2_cleaning_report
ENGINE = MergeTree
ORDER BY feature
AS
/*__COLUMN_PROFILE_SELECTS__*/;
