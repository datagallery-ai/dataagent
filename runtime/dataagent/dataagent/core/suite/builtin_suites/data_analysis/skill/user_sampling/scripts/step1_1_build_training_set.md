# step1_1：构造抽样池

`prelabeled` 和 `regular` 都执行本步。读取 `step1_0_table_schema.json` 和 `step1_0_sampling_plan.json`。比例、表名和过滤条件都从 plan 读取。

抽样上限：

- `draw_size = sample_size + floor(sample_size / 4)`
- 正样本上限 = `floor(draw_size * positive / (positive + negative))`
- 负样本上限 = 实际抽到的正样本人数 × `negative / positive`
- 正负样本合计不超过 `draw_size`

按 plan 的 `mode` 选择下面一个模板，再按 `resources/logical_sql.md` 编译。编译结果通过 `data_source.md` 的 `<resource_id>` 提交。产物表是 `{{output_database}}.step1_temp_sampled_pool`，列是 `user_key`、`label`。

## prelabeled

正负样本都来自 `sampling_sources.user_table` 的 `keys.label_column`。正值、负值用 plan 的 `label_values`。

```sql
<LOGICAL_MATERIALIZE target="{{output_database}}.step1_temp_sampled_pool">
WITH
pos AS (
  SELECT DISTINCT <user_key_expr> AS user_key
  FROM {{source_database}}.<user_table>
  WHERE <valid_user>
    AND <label_column> = <label_values.positive>
),
pos_limited AS (
  <LOGICAL_TAKE count="<positive_cap>" order_by="<LOGICAL_STABLE_HASH(user_key)>">
  SELECT user_key FROM pos
),
neg AS (
  SELECT DISTINCT <user_key_expr> AS user_key
  FROM {{source_database}}.<user_table>
  WHERE <valid_user>
    AND <label_column> = <label_values.negative>
),
neg_sampled AS (
  <LOGICAL_TAKE
    count="<LOGICAL_ROW_COUNT(pos_limited)> * <ratio.negative> / <ratio.positive>"
    order_by="<LOGICAL_STABLE_HASH(user_key)>">
  SELECT user_key FROM neg
)
SELECT user_key, max(label) AS label
FROM (
  SELECT user_key, 1 AS label FROM pos_limited
  UNION ALL
  SELECT user_key, 0 AS label FROM neg_sampled
) AS combined
GROUP BY user_key
```

同一用户同时出现在两侧时，`max(label)` 保留 1。

## regular

正样本来自 label 窗口内对目标实体发生目标行为的用户。候选池和负样本按 `resources/candidate_pool.md`、`resources/negative_samples.md` 构造。`neg_pool` 只合并 plan.`negative_populations` 里列出的群体。

```sql
<LOGICAL_MATERIALIZE target="{{output_database}}.step1_temp_sampled_pool">
WITH
pos AS (
  SELECT DISTINCT <user_key_expr> AS user_key
  FROM {{source_database}}.<y_label.event_table>
  WHERE <valid_user>
    AND <entity_filter>
    AND <positive_label>
    AND <label_window>
),
pos_limited AS (
  <LOGICAL_TAKE count="<positive_cap>" order_by="<LOGICAL_STABLE_HASH(user_key)>">
  SELECT user_key FROM pos
),
neg_pool AS (
  SELECT user_key FROM <plan 中启用的负样本群体>
),
neg_sampled AS (
  <LOGICAL_TAKE
    count="<LOGICAL_ROW_COUNT(pos_limited)> * <ratio.negative> / <ratio.positive>"
    order_by="<LOGICAL_STABLE_HASH(user_key)>">
  SELECT user_key FROM neg_pool
)
SELECT user_key, max(label) AS label
FROM (
  SELECT user_key, 1 AS label FROM pos_limited
  UNION ALL
  SELECT user_key, 0 AS label FROM neg_sampled
) AS combined
GROUP BY user_key
```

同一用户同时出现在正负样本中时，`max(label)` 保留 1。

池表可查之后，读取 `scripts/step1_2_build_test_set.md`。
