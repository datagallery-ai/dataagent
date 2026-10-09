# 口径：labels（通用正样本）

**目的**：构造 label 窗口内的正样本。family 的关键词和负样本策略来自
工作区里的 `scene.yaml`（来自 `config/scenes/<scene_name>.yaml`）；本文件不维护业务词表、固定表名、固定列名或枚举值。

正样本是在 `(T0, T0+label_window_days]` 内，对 `entity_scope` 指定实体发生
目标行为的 canonical `user_key`。表、列、类型和谓词只从 schema 与 plan 的
`sampling_sources`、`keys`、`sql_fragments` 读取。

## 单事件正样本

```text
SELECT DISTINCT <user_key_expr> AS user_key
FROM {{source_database}}.<y_label.event_table>
WHERE <valid_user>
  AND <entity_filter>
  AND <positive_label>
  AND <label_window>;
```

`positive_label` 由 step1_0 查询事件表的真实取值后写入 plan。语义结果用来理解列的含义，取值本身来自这次查询。

## 多事件链路

仅当语义结果和 `join_hints` 明确给出链路时使用：

```text
SELECT DISTINCT <left_user_key_expr> AS user_key
FROM {{source_database}}.<upstream_event> AS e
INNER JOIN {{source_database}}.<downstream_event> AS d
  ON <verified_user_join> AND <verified_entity_join>
WHERE <valid_join_keys>
  AND <entity_filter>
  AND <upstream_window>
  AND <downstream_positive_label>
  AND <downstream_window>
  AND <downstream_after_upstream>;
```

## 聚合阈值

次数、天数、金额或时长阈值必须来自任务参数或真实画像，并固化到
`sql_fragments.positive_label`：

```text
SELECT <user_key_expr> AS user_key
FROM {{source_database}}.<y_label.event_table>
WHERE <valid_user> AND <entity_filter> AND <label_window>
GROUP BY user_key
HAVING <verified_aggregate_predicate>;
```

输出一列 canonical `user_key`，供 `step1_1_build_training_set.md` 的 `pos`
使用。标签是二分类：正样本为 1，负样本为 0。
