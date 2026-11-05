-- step2_2: 聚合宽表用户键门禁（单条 SELECT）
-- 本文件必须在 step2_2_wide_aggregated CREATE 完成后独立提交。

SELECT
    count() AS n_rows,
    uniqExact(<user_id>) AS n_user_id,
    count() = uniqExact(<user_id>) AS user_key_unique,
    (
        SELECT count()
        FROM {{output_database}}.step2_1_wide_joined
    ) AS expected_rows,
    n_rows = expected_rows AS row_count_unchanged
FROM {{output_database}}.step2_2_wide_aggregated
FORMAT JSON;

-- step2_2 字符串高基数门禁（独立提交，单条 SELECT）
-- 检查 step2_2_wide_aggregated 中是否残留未转换的 String / Nullable(String)。
-- LowCardinality(String) 是允许的落点（版本等非数值分类）。地理原列不行：必须只留 *_tier。
-- 门禁改为返回 countIf 计数：始终返回单行 JSON，命中列数为 0 才通过，
-- 避免「预期无结果」查询返回空结果集触发非 JSON 误报（0916 step2_2 门禁耗 57s）。
--
-- 占位符绑定规范：本模板的三个动态块只由 `step2_2_driver.py finalize` 绑定。
-- **禁止手工 sed、禁止提交含 /*__...__*/ 的版本**。
--   /*__LIST_FIELD_NAMES__*/     = list_fields.txt 的带引号字段列表
--   /*__ALLOWED_STRING_COLUMNS__*/ = 物理 schema 中全部 String 列（本查询的放行清单）
--
-- 放行清单语义：`AND name NOT IN (...)` 里的列不触发门禁（1:1/维度表字符串列原样透传）。
-- 宽表新产出的派生 String 列（如 `{n_to_one_table}_<col>`）不在清单内，必须先经
-- `string_cast_plan.txt` 采样转换，否则本查询会命中它。

SELECT
    countIf(name IN (/*__LIST_FIELD_NAMES__*/)) AS n_list_fields_leaked,
    countIf(
        name NOT IN (/*__LIST_FIELD_NAMES__*/)
        AND (
            type IN ('String', 'Nullable(String)')
            OR (
                position(type, 'String') > 0
                AND position(type, 'LowCardinality') = 0
            )
        )
    ) AS n_unconverted_string_columns
FROM <catalog_columns>
WHERE database = '{{output_database}}'
  AND table = 'step2_2_wide_aggregated'
  AND name NOT IN (/*__ALLOWED_STRING_COLUMNS__*/)
FORMAT JSON;

-- step2_2 地理原列门禁（独立提交，单条 SELECT）
-- step2_2_wide_aggregated / 最终 CSV 只允许 {col}_tier。
-- 列名匹配规则只存在于 references/scene.yaml 的 geo_columns；本模板禁止写死场景列名。
-- /*__GEO_COLUMN_MATCH__*/ 由 `step2_2_driver.py finalize` 调用
-- `step2_2_city_tier_sql.py --geo-match-sql` 绑定，**禁止手工替换**。
-- 门禁改为返回 countIf 计数：始终返回单行 JSON，泄漏列数为 0 才通过。

SELECT
    countIf(name NOT LIKE '%_tier' AND (/*__GEO_COLUMN_MATCH__*/)) AS n_geo_columns_leaked
FROM <catalog_columns>
WHERE database = '{{output_database}}'
  AND table = 'step2_2_wide_aggregated'
FORMAT JSON;
