-- step2_1: 全量源字段画像（ClickHouse 单语句动态模板）
-- 输入: schema_resolution.source_tables 中全部业务源表及系统列清单
-- 输出: {{database}}.step2_1_column_profile
-- 动态块必须在提交前展开；每个字段生成一个 SELECT，并以 UNION ALL 连接。
-- 本文件整体必须作为一条独立 ClickHouse MCP command 提交。

CREATE OR REPLACE TABLE {{database}}.step2_1_column_profile
ENGINE = MergeTree
ORDER BY (table_name, column_name)
AS
/*__SOURCE_COLUMN_PROFILE_SELECTS__*/;
