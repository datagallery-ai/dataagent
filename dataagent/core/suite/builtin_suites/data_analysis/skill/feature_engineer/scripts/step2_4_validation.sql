-- step2_4: 最终宽表标签与用户键门禁（单条 SELECT）
-- 本文件必须在 step2_4_wide_userfiltered CREATE 完成后独立提交。

SELECT
    count() AS n_rows,
    uniqExact(<user_id>) AS n_users,
    count() = uniqExact(<user_id>) AS user_key_unique,
    countIf(<label> IS NULL OR toString(<label>) = '') AS n_label_missing,
    countIf(toString(<label>) NOT IN ('0', '1')) AS n_label_invalid,
    countIf(toString(<label>) = '1') AS n_pos,
    countIf(toString(<label>) = '0') AS n_neg
FROM {{output_database}}.step2_4_wide_userfiltered;

-- step2_4 列对账门禁（独立提交，单条 SELECT）
-- step2_4 只过滤行，不改变列——最终列数应与 step2_3_wide_complete 一致。
-- 因此直接对照 step2_3_wide_complete 的列数，无需 agent 手工计数。

SELECT
    (SELECT count()
     FROM system.columns
     WHERE database = '{{output_database}}'
       AND table = 'step2_4_wide_userfiltered') AS actual_cols,
    (SELECT count()
     FROM system.columns
     WHERE database = '{{output_database}}'
       AND table = 'step2_3_wide_complete') AS expected_cols,
    actual_cols = expected_cols AS column_reconciled
FORMAT TSVWithNames;
