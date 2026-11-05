-- step2_3: 聚合宽表用户级清洗（`<warehouse>`）
-- 输入: step2_2_wide_aggregated
-- 输出: {{output_database}}.step2_3_wide_cleaned
-- 本文件整体作为一条独立 `<warehouse>` MCP command 提交；EXCEPT 子句由
-- step2_3_expand_sql.py 确定性展开（禁止手写、禁止改动本文件）。

CREATE OR REPLACE TABLE {{output_database}}.step2_3_wide_cleaned
ENGINE = MergeTree
ORDER BY <user_id>
SETTINGS allow_nullable_key = 1
AS
SELECT * /*__CLEANING_EXCEPT_CLAUSE__*/
FROM {{output_database}}.step2_2_wide_aggregated;
