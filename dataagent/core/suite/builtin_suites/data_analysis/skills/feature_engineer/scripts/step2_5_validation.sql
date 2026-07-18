-- step2_5: 最终宽表标签与用户键门禁（单条 SELECT）
-- 本文件必须在 step2_5_wide_userfiltered CREATE 完成后独立提交。

SELECT
    count() AS n_rows,
    uniqExact(<user_id>) AS n_users,
    count() = uniqExact(<user_id>) AS user_key_unique,
    countIf(<label> IS NULL OR toString(<label>) = '') AS n_label_missing,
    countIf(toString(<label>) NOT IN ('0', '1')) AS n_label_invalid,
    countIf(toString(<label>) = '1') AS n_pos,
    countIf(toString(<label>) = '0') AS n_neg
FROM {{database}}.step2_5_wide_userfiltered;
