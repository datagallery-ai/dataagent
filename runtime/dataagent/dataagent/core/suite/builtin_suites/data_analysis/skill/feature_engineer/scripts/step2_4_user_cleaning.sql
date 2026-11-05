-- step2_4: 用户清洗（`<warehouse>`）
-- 过滤 <age> / <gender> 为空或空字符串的用户
-- 输入: step2_3_wide_cleaned
-- 输出: step2_4_wide_userfiltered
-- 本模板只由 step2_4_driver.py prepare 展开绑定，禁止手工替换占位符。

CREATE OR REPLACE TABLE {{output_database}}.step2_4_wide_userfiltered
ENGINE = MergeTree
ORDER BY <user_id>
SETTINGS allow_nullable_key = 1
AS
SELECT *
FROM {{output_database}}.step2_3_wide_cleaned
WHERE <age> IS NOT NULL
  AND toString(<age>) != ''
  AND <gender> IS NOT NULL
  AND toString(<gender>) != '';
