-- step2_0: 全量源表画像（ClickHouse 单语句动态模板）
-- 输入: schema_resolution.source_tables 中全部业务源表
-- 输出: {{output_database}}.step2_0_table_profile
-- 动态块必须在提交前展开，不得把 /*__...__*/ 原样发送到 ClickHouse。
-- 本文件整体必须作为一条独立 ClickHouse MCP command 提交。

CREATE OR REPLACE TABLE {{output_database}}.step2_0_table_profile
ENGINE = MergeTree
ORDER BY table_name
AS
-- 为 source_tables 中每张表生成一个 SELECT，并以 UNION ALL 连接。
-- 无 <user_id> 的维表/未使用表将 unique_user_id 写为 NULL。
/*__SOURCE_TABLE_PROFILE_SELECTS__*/;
