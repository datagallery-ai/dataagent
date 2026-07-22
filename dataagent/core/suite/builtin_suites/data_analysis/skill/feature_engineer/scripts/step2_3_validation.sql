-- step2_3: 聚合宽表用户键门禁（单条 SELECT）
-- 本文件必须在 step2_3_wide_complete CREATE 完成后独立提交。

SELECT
    count() AS n_rows,
    uniqExact(<user_id>) AS n_user_id,
    count() = uniqExact(<user_id>) AS user_key_unique,
    (
        SELECT count()
        FROM {{output_database}}.step2_2_wide_cleaned
    ) AS expected_rows,
    n_rows = expected_rows AS row_count_unchanged
FROM {{output_database}}.step2_3_wide_complete;
