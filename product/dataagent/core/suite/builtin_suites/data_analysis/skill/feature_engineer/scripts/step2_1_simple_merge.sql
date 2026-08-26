-- step2_1: 动态 1:1 合表（ClickHouse）
-- 输入: <user_table> + step2_0 分类出的任意数量 one_to_one 表
-- 输出: {{output_database}}.step2_1_wide_simple
-- 动态块由 step2_0 画像展开；右表键不唯一时必须先阻塞，不能静默去重。
-- 本文件整体必须作为一条独立 ClickHouse MCP command 提交。
--
-- !!! 前置要求：key_validation 检查 !!!
-- 展开 JOIN 前必须 read_file 读取 schema_resolution.json 的 key_validation 字段，
-- 检查每张 1:1 右表的 max_duplication_factor。>1 的表必须先用 CTE 去重再 JOIN。
--
-- ⛔ 禁止逐列显式 SELECT。必须使用 `u.*` 取基表字段，禁止写成 u.usid, u.label, u.age, ...。
--   使用 u.* 时 <label> 自然紧随 <user_id> 处于第 2 列——无需 label 重排修正之旅。

CREATE OR REPLACE TABLE {{output_database}}.step2_1_wide_simple
ENGINE = MergeTree
ORDER BY <user_id>
AS
SELECT
    u.*
    /*__ONE_TO_ONE_SELECT_COLUMNS__*/
FROM {{output_database}}.<user_table> AS u
/*__ONE_TO_ONE_JOIN_BLOCKS__*/;
