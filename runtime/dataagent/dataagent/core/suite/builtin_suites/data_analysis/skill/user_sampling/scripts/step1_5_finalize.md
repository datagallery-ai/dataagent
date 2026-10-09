# step1_5：定稿

本步读取 plan、schema、`step1_sample_stats.json` 和工作区 `data_source.md`，写出 `step1_output_meta.json` 和 `receipt.json`。本步不再提交 SQL。

## 1. 核对

按这个顺序核对。每一项都通过后，才写两个最终文件。

| 核对项 | 通过条件 |
|---|---|
| plan | 含有 `scene_id`、`entity_scope.target`、`sampling_config`，并且 `inventory_check.ok` 为 true |
| schema | 每张源表都有列，并且含有 `join_hints`、`role_candidates`、`column_aliases` |
| 交付表数量 | `table_count_check.ok` 为 true，`missing_tables` 为空 |
| 测试集 | `test_set_stats` 存在，`train_overlap` 为 0，产物库里有 `step1_test_users` |
| 同名表 | `output_database` 里与源表同名的表数等于 `inventory_check.table_count` |
| 用户表标签 | `regular` 在源列之外有一列 `label`；`prelabeled` 保留源表的标签列 |
| 实体表 | 每张 `entity_keyed` 交付表都按 `entity_filter` 保留了目标实体 |

有一项未通过时，回到对应步骤修好再重跑本步。

## 2. 写出 schema 交接文件

把磁盘上的 `step1_0_table_schema.json` 原样写为 `step1_output_meta.json`。统计数字留在 `step1_sample_stats.json`。

## 3. 写出 receipt

`<table_kind>` 和 `<uri_scheme>` 用工作区 `data_source.md` 的当前取值。receipt 顶层是 `summary` 和 `artifacts`。

登记这三类产物：

1. `step1_output_meta.json`，kind 为 `file`，type 为 `meta`。
2. `step1_sample_stats.json`，kind 为 `file`，type 为 `stats`。
3. `step1_test_users`，以及 `projection_tables` 里的每张同名交付表。表产物写成：

```json
{
  "kind": "<table_kind>",
  "uri": "<uri_scheme>://<output_database>/<table>",
  "name": "<output_database>.<table>"
}
```

`summary` 概括本次采样。其中的表数、训练用户数、mode 和产物库与 `step1_sample_stats.json` 一致。`step1_temp_*` 是过程表，receipt 只登记上面三类产物。
