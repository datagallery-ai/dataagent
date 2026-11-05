-- step2_4: 最终宽表总行数（单条 SELECT，供 export-plan 计算批次）
-- 本文件必须在 step2_4_wide_userfiltered CREATE 完成后独立提交。
-- 占位符由 step2_4_driver.py prepare 绑定，禁止手工替换。

SELECT count() AS n_rows
FROM {{output_database}}.step2_4_wide_userfiltered
FORMAT JSON;
