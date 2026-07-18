-- step2_3: 动态全字段特征清洗（ClickHouse）
-- 输入: step2_2_wide_simple 的实际列清单
-- 输出: {{database}}.step2_3_wide_cleaned
-- 动态块必须覆盖 step2_2 的全部字段；<user_id>/<label>/<age>/<gender> 为保护字段。
-- 本文件整体必须作为一条独立 ClickHouse MCP command 提交。

CREATE OR REPLACE TABLE {{database}}.step2_3_wide_cleaned
ENGINE = MergeTree
ORDER BY <user_id>
AS
SELECT * /*__CLEANING_EXCEPT_CLAUSE__*/
FROM {{database}}.step2_2_wide_simple;
