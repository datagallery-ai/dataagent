-- step2_4: 最终宽表列对账门禁（单条 SELECT）
-- 本文件必须在 step2_4_stats_validation.sql 之后独立提交。
-- step2_4 只过滤行，不改变列——最终列数应与 step2_3_wide_cleaned 一致。
-- 因此直接对照 step2_3_wide_cleaned 的列数，不要手工计数。
-- <catalog_columns> 由 step2_4_driver.py prepare 绑定，禁止手工替换。

SELECT
    (SELECT count()
     FROM <catalog_columns>
     WHERE database = '{{output_database}}'
       AND table = 'step2_4_wide_userfiltered') AS actual_cols,
    (SELECT count()
     FROM <catalog_columns>
     WHERE database = '{{output_database}}'
       AND table = 'step2_3_wide_cleaned') AS expected_cols,
    actual_cols = expected_cols AS column_reconciled
FORMAT JSON;
