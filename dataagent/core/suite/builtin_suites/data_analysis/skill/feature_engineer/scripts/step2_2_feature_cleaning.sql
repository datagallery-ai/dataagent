-- step2_2: 动态全字段特征清洗（ClickHouse）
-- 输入: step2_1_wide_simple 的实际列清单
-- 输出: {{output_database}}.step2_2_wide_cleaned
-- 清洗决策引用 step2_0 字段画像结果：常量候选直接删除，semantic_constant 标记的字段直接按语义常量处理。
-- 动态块必须覆盖 step2_1 的全部字段；<user_id>/<label>/<age>/<gender> 为保护字段。
-- 本文件整体必须作为一条独立 ClickHouse MCP command 提交。

CREATE OR REPLACE TABLE {{output_database}}.step2_2_wide_cleaned
ENGINE = MergeTree
ORDER BY <user_id>
AS
SELECT * /*__CLEANING_EXCEPT_CLAUSE__*/
FROM {{output_database}}.step2_1_wide_simple;
