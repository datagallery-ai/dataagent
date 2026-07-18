-- step2_2: 动态 1:1 合表（ClickHouse）
-- 输入: <user_table> + step2_1 分类出的任意数量 one_to_one 表
-- 输出: {{database}}.step2_2_wide_simple
-- 动态块由 step2_1 画像展开；右表键不唯一时必须先阻塞，不能静默去重。
-- 本文件整体必须作为一条独立 ClickHouse MCP command 提交。

CREATE OR REPLACE TABLE {{database}}.step2_2_wide_simple
ENGINE = MergeTree
ORDER BY <user_id>
AS
SELECT
    u.*
    /*__ONE_TO_ONE_SELECT_COLUMNS__*/
FROM {{database}}.<user_table> AS u
/*__ONE_TO_ONE_JOIN_BLOCKS__*/;
