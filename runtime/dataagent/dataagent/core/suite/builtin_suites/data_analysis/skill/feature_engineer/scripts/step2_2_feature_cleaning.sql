-- step2_2: 动态全字段特征清洗（ClickHouse）
-- 输入: step2_1_wide_simple 的实际列清单
-- 输出: {{output_database}}.step2_2_wide_cleaned
-- 清洗决策引用 step2_0 字段画像结果：常量候选直接删除，semantic_constant 标记的字段直接按语义常量处理。
-- 动态块必须覆盖 step2_1 的全部字段；<user_id>/<label>/<age>/<gender> 为保护字段。
-- 本文件整体必须作为一条独立 ClickHouse MCP command 提交。
--
-- ⛔ /*__CLEANING_EXCEPT_CLAUSE__*/ 展开规范：
--   必须以 step2_2_cleaning_report 的 DROP 字段与 step2_1 列的交集为依据，用 EXCEPT (...) 排除。
--   cleaning_report 覆盖所有源表（含 1:N），但 step2_1 只含 1:1 表。
--   EXCEPT 列表 = {DROP 字段} ∩ {step2_1 实际列}，取交集。
--   禁止手工列出字段名。
--
--   EXCEPT 展开的正确流程：
--   1. 查询 cleaning_report 的 DROP 字段：SELECT feature FROM step2_2_cleaning_report WHERE recommendation = 'DROP'
--   2. 查询 step2_1 的实际列：SELECT name FROM system.columns WHERE database='{输出库}' AND table='step2_1_wide_simple'
--   3. 取交集展开为：EXCEPT (col1, col2, ...)
--   4. 若交集为空（DROP 字段都不在 step2_1 中，如常量已被 step2_1 显式排除）→ EXCEPT 子句可为空，SQL 语义为空但仍需提交执行
--   5. 1:N 表的 DROP 字段不在本次 EXCEPT 范围——它们在 step2_3 聚合前由 cleaning_report 过滤

CREATE OR REPLACE TABLE {{output_database}}.step2_2_wide_cleaned
ENGINE = MergeTree
ORDER BY <user_id>
AS
SELECT * /*__CLEANING_EXCEPT_CLAUSE__*/
FROM {{output_database}}.step2_1_wide_simple;
