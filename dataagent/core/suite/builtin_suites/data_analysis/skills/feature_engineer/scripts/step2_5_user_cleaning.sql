-- step2_5: 用户清洗（ClickHouse）
-- 过滤 <age> / <gender> 为空或空字符串的用户
-- 输入: step2_4_wide_complete
-- 输出: step2_5_wide_userfiltered
-- 本文件整体必须作为一条独立 ClickHouse MCP command 提交。

CREATE OR REPLACE TABLE {{database}}.step2_5_wide_userfiltered
ENGINE = MergeTree
ORDER BY <user_id>
AS
SELECT *
FROM {{database}}.step2_4_wide_complete
WHERE <age> IS NOT NULL
  AND toString(<age>) != ''
  AND <gender> IS NOT NULL
  AND toString(<gender>) != '';
