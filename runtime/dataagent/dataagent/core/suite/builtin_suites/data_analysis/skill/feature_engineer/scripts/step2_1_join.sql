-- step2_1: 1:1 合表
-- 输入: <user_table> + step2_0 分类出的全部 `1:1用户表`
-- 输出: {{output_database}}.step2_1_wide_joined
-- 由 step2_1_expand_sql.py 展开后提交；本文件整体作为一条独立 `<warehouse>` MCP command 提交。

CREATE OR REPLACE TABLE {{output_database}}.step2_1_wide_joined
ENGINE = MergeTree
ORDER BY <user_id>
AS
/*__ONE_TO_ONE_MERGE_BODY__*/;
