-- step2_1: 合表行数与用户键门禁（单条 SELECT）
-- 本文件必须在 step2_1_wide_simple CREATE 完成后独立提交。

SELECT
    count() AS n_rows,
    uniqExact(<user_id>) AS n_user_id,
    count() = uniqExact(<user_id>) AS user_key_unique,
    (
        SELECT count()
        FROM {{output_database}}.<user_table>
    ) AS expected_rows,
    n_rows = expected_rows AS row_count_unchanged
FROM {{output_database}}.step2_1_wide_simple;
