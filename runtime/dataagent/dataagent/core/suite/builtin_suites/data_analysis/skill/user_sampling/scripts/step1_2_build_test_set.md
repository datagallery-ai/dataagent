# step1_2：切分训练用户和测试用户

`prelabeled` 和 `regular` 都执行本步。读取 plan，在每个 `label` 内按 `<LOGICAL_STABLE_HASH(user_key)>` 排序。

训练配额用原来的 `sample_size`，不用 step1_1 的 `draw_size`：

- `train_pos_cap = floor(sample_size * positive / (positive + negative))`
- `train_neg_cap = train_pos_cap * negative / positive`

先从 `step1_temp_sampled_pool` 取出训练配额，写入 `{{output_database}}.step1_temp_sampled_users`。正样本取前 `train_pos_cap` 人，负样本取前 `train_neg_cap` 人。某一侧人数少于配额时，该侧有多少取多少。

再把池中剩下的用户写入 `{{output_database}}.step1_test_users`：

```sql
<LOGICAL_MATERIALIZE target="{{output_database}}.step1_test_users">
SELECT p.user_key, p.label
FROM {{output_database}}.step1_temp_sampled_pool AS p
<LOGICAL_ANTI_JOIN
  right="{{output_database}}.step1_temp_sampled_users AS t"
  on="p.user_key = t.user_key">
```

两段 SQL 都先按 `resources/logical_sql.md` 编译，再提交。两张表的列都是 `user_key`、`label`。

`step1_temp_sampled_users` 供 step1_3 过滤训练集。`step1_test_users` 保留到定稿，并登记进 receipt。step1_4 会查询两张表的用户交集，把人数写入 `test_set_stats.train_overlap`。

两张表都可查之后，读取 `scripts/step1_3_project_tables.md`。
