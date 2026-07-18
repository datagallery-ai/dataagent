-- step2_3: 动态全字段清洗决策画像（ClickHouse 单语句动态模板）
-- 输入: {{database}}.step2_2_wide_simple 的实际列清单
-- 输出: {{database}}.step2_3_cleaning_report
-- 每个实际字段生成一个 SELECT，以 UNION ALL 连接；recommendation 只允许 KEEP 或 DROP。
-- 本文件整体必须作为一条独立 ClickHouse MCP command 提交。

CREATE OR REPLACE TABLE {{database}}.step2_3_cleaning_report
ENGINE = MergeTree
ORDER BY feature
AS
/*__COLUMN_PROFILE_SELECTS__*/;
