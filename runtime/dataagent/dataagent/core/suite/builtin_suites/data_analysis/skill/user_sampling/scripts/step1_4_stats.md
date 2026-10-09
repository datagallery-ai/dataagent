# step1_4：统计

读取 schema 和 plan。先查询，再按下面的条件核对，最后把查询结果写入 `step1_sample_stats.json`。库名、`target`、`mode` 和配置比例从 plan 原样抄入。

## 1. 训练用户

统计投影类型为 `user_table` 的那张交付表：总行数、去重用户数、正样本人数、负样本人数，以及正样本人数除以负样本人数。

`regular` 的标签列是投影时追加的 `label`，正值是 1，负值是 0。`prelabeled` 的标签列和正负值用 plan 的 `keys.label_column`、`label_values`。

## 2. 交付表张数

用 `<LOGICAL_COUNT_TABLES>` 统计 `output_database` 中名称不匹配 `step1_%` 的表。期望值是 `inventory_check.table_count`。`step1_temp_*` 和 `step1_test_users` 都带 `step1_` 前缀，不计入这个数量。对不上的源表名写入 `missing_tables`。

## 3. 每张交付表的行数

对 `projections[]` 里每张表统计行数，记入 `projection_tables`。`user_table` 额外写 `has_label: true`。

## 4. 测试集和重叠人数

统计 `step1_test_users` 的正样本人数、负样本人数和总人数。再统计它和训练集的共同用户数：

```sql
SELECT <LOGICAL_COUNT_DISTINCT(train.user_key)> AS train_overlap
FROM {{output_database}}.step1_temp_sampled_users AS train
JOIN {{output_database}}.step1_test_users AS test
  ON train.user_key = test.user_key
```

这四项写入 `test_set_stats`。数字来自查询结果。

## 5. 通过条件

同时满足这些条件才写 `step1_sample_stats.json`：

| 检查 | 通过条件 | 未通过时 |
|---|---|---|
| 用户去重 | 用户表的去重人数等于总行数 | 回到 step1_3 |
| 训练规模 | 训练用户数小于或等于 `sample_size` | 回到 step1_2 |
| 正样本 | 正样本人数大于 0 | 回到 step1_1 |
| 正负比 | 实际比例相对 `positive / negative` 的偏差不超过 `tolerance` | 回到 step1_1 |
| 明细表规模 | 先数源表行数。源表有行时，`user_keyed` 交付行数不超过源表行数的一半 | 回到 step1_3 |
| 交付表数 | `table_count_check.ok` 为 true，`missing_tables` 为空 | 回到 step1_3 |
| 训练和测试 | `test_set_stats.train_overlap` 为 0 | 回到 step1_2 |

`test_set_stats` 的结构：

```json
{
  "positive": "<测试集正样本人数>",
  "negative": "<测试集负样本人数>",
  "total": "<测试集总人数>",
  "train_overlap": 0
}
```

统计文件还包含 `scene_id`、`run_id`、`source_database`、`output_database`、`entity_scope.target`、`T0`、时间窗、`sample_size`、`actual_sample_size`、`mode`、配置与实际正负比、`label_stats`、`table_count_check` 和 `projection_tables`。`entity_scope.target` 和 `mode` 从 plan 原样抄入，`mode` 是 `regular` 或 `prelabeled`。

写好后读取 `scripts/step1_5_finalize.md`。
