-- step2_0: 全量源表画像（ClickHouse 单语句动态模板）
-- 输入: schema_resolution.source_tables 中全部业务源表
-- 输出: {{output_database}}.step2_0_table_profile
-- 动态块必须在提交前展开，不得把 /*__...__*/ 原样发送到 ClickHouse。
-- 本文件整体必须作为一条独立 ClickHouse MCP command 提交。
--
-- ⛔ 每个表的 SELECT 必须包含 classification 列——用硬 SQL 规则计算，禁止 LLM 手工判断：
--
--   CASE
--       WHEN uniqExact(<user_id>) IS NULL THEN '维度表'
--       WHEN uniqExact(<user_id>) = count() THEN '1:1用户表'
--       ELSE '1:N行为表'
--   END AS classification
--
--   铁律：n_unique(主键) = 行数 ↔ 1:1。
--   反面案例：`list_detail_info` (行数=30000, usid n_unique=30000) 和 `game_statistics_push` (行数=61691, usid n_unique=61691)
--   被 LLM 误判为 1:N 行为表，做了多余的 GROUP BY 聚合。硬 SQL 规则会正确分类为 1:1用户表。
--
--   展开后的每表 SELECT 必须包含以下列：
--     'table_name' AS table_name,
--     count() AS total_rows,
--     uniqExact(<user_id>) AS unique_user_id,
--     <上面 CASE WHEN 三行> AS classification  ← 必须包含

CREATE OR REPLACE TABLE {{output_database}}.step2_0_table_profile
ENGINE = MergeTree
ORDER BY table_name
AS
-- 为 source_tables 中每张表生成一个 SELECT，并以 UNION ALL 连接。
-- 无 <user_id> 的维表/未使用表将 unique_user_id 写为 NULL。
/*__SOURCE_TABLE_PROFILE_SELECTS__*/;
