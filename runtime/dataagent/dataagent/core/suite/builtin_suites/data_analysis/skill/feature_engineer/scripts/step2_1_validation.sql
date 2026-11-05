-- step2_1: 合表行数、用户键与列对账门禁（单条 SELECT）
-- 本文件必须在 step2_1_wide_joined CREATE 完成后独立提交。
-- 期望列清单由 step2_1_expand_sql.py 以字面量内联（禁止手写），列对账在仓内完成；
-- 结果由 step2_1_verify_gate.py 判定，禁止肉眼比对。
-- <catalog_columns> 由 step2_1_driver.py 展开时按工作区 data_source.json 自动绑定为当前仓列元数据表。

SELECT
    count() AS n_rows,
    uniqExact(<user_id>) AS n_user_id,
    count() = uniqExact(<user_id>) AS user_key_unique,
    (SELECT count() FROM {{output_database}}.<user_table>) AS expected_rows,
    count() = (SELECT count() FROM {{output_database}}.<user_table>) AS row_count_unchanged,
    length(/*__EXPECTED_COLUMNS_LITERAL__*/) AS expected_column_count,
    (SELECT count()
       FROM <catalog_columns>
      WHERE database = '{{output_database}}'
        AND table = 'step2_1_wide_joined') AS actual_column_count,
    arraySort(
        (SELECT groupUniqArray(name)
           FROM <catalog_columns>
          WHERE database = '{{output_database}}'
            AND table = 'step2_1_wide_joined')
    ) = arraySort(/*__EXPECTED_COLUMNS_LITERAL__*/) AS column_reconciliation_ok
FROM {{output_database}}.step2_1_wide_joined
FORMAT JSON;
