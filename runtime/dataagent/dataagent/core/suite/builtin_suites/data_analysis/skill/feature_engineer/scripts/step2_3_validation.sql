-- step2_3: 清洗结果完整性门禁（单条 SELECT）
-- 本文件必须在 cleaning_report 与 wide_cleaned 两个 CREATE 完成后独立提交。
-- EXCEPT 字段清单由 step2_3_expand_sql.py 以字面量内联（禁止手写）；
-- 「DROP 残留」「列覆盖」判定结果由 step2_3_verify_gate.py 给出。
-- <catalog_columns> 由 step2_3_driver.py 展开时按工作区 data_source.json 自动绑定为当前仓列元数据表。

SELECT
    count() AS n_rows,
    uniqExact(<user_id>) AS n_user_id,
    count() = uniqExact(<user_id>) AS user_key_unique,
    -- 清洗决策必须覆盖聚合宽表的每一列
    (SELECT count() FROM {{output_database}}.step2_3_cleaning_report) AS profiled_columns,
    (SELECT count()
       FROM <catalog_columns>
      WHERE database = '{{output_database}}'
        AND table = 'step2_2_wide_aggregated') AS expected_columns,
    (SELECT count() FROM {{output_database}}.step2_3_cleaning_report)
        = (SELECT count()
             FROM <catalog_columns>
            WHERE database = '{{output_database}}'
              AND table = 'step2_2_wide_aggregated') AS column_coverage_ok,
    (SELECT count()
       FROM <catalog_columns>
      WHERE database = '{{output_database}}'
        AND table = 'step2_3_wide_cleaned'
        AND name IN /*__EXCEPT_COLUMNS_LITERAL__*/) AS surviving_except_columns,
    (SELECT count()
       FROM <catalog_columns>
      WHERE database = '{{output_database}}'
        AND table = 'step2_3_wide_cleaned'
        AND name IN (SELECT feature
                       FROM {{output_database}}.step2_3_cleaning_report
                      WHERE recommendation = 'DROP')) AS surviving_drop_features,
    -- 保护列不得被清洗掉
    (SELECT count()
       FROM {{output_database}}.step2_3_cleaning_report
      WHERE recommendation = 'DROP'
        AND feature IN (/*__PROTECTED_COLUMNS__*/)) AS dropped_protected_columns
FROM {{output_database}}.step2_3_wide_cleaned
FORMAT JSON;
