# 口径：negative_samples（多群体负样本）

群体选择来自 plan.`sampling_config.label_family`，SQL 由
`step1_1_build_training_set.md` 组合。所有群体排除正样本；每群体不限量，
总量由 step1_1 的 `<LOGICAL_TAKE>` 统一控制。稳定抽样、anti join 和分位数按
`logical_sql.md` 编译。表列只来自 plan 的 `sampling_sources`、`keys`、
`sql_fragments`。

| 代号 | 群体 | 口径 |
|---|---|---|
| N1 | 域内未转化 | 候选池排除正样本 |
| N2 | 曝光未转化 | 曝光目标实体但无下游转化 |
| N3 | 高活跃不碰目标 | lookback 高分位活跃且不碰目标 |
| N4 | 全局转化但未转化目标 | 全局发生目标类行为但未对目标实体转化 |
| N5 | 随机背景 | 用户全集稳定抽样并排除正样本 |
| N6 | 兴趣错配 | 画像与目标实体不同且非正样本 |

写入 `neg_pool` 的群体来自 plan 的 label family，并且 schema 里已经有对应的事件表和列。表名、列名从 plan 的 `sampling_sources` 读取。

## N1

```text
SELECT p.user_key
FROM pool AS p
<LOGICAL_ANTI_JOIN right="pos AS pos_ex" on="p.user_key = pos_ex.user_key">
```

## N2

```text
SELECT <canonical_user_key_exposure> AS user_key
FROM {{source_database}}.<exposure_event> AS e
<LOGICAL_ANTI_JOIN right="pos AS pos_ex"
  on="<canonical_user_key_exposure> = pos_ex.user_key">
WHERE <valid_user_key_predicate_exposure>
  AND <entity_filter_predicate_exposure>
  AND <negative_exposure_window_predicate>
  AND <exposure_not_converted_predicate>
```

## N3

先构造目标实体触达用户；再按用户统计 lookback 活跃量，保留
`<LOGICAL_PERCENTILE(act_cnt, 0.9)>` 以上，并对触达用户及正样本分别执行
`<LOGICAL_ANTI_JOIN>`。

## N4

从全局命中目标类行为的用户中，anti join 目标实体转化用户和正样本。目标实体
条件必须使用 plan 的 `entity_filter`。

## N5

```text
SELECT <canonical_background_user_key> AS user_key
FROM {{source_database}}.<background_user_source> AS u
<LOGICAL_ANTI_JOIN right="pos AS pos_ex"
  on="<canonical_background_user_key> = pos_ex.user_key">
WHERE <valid_background_user_key_predicate>
```

`background_user_source` 必须是语义层解析出的用户全集来源。

## N6

N5 口径再加 `<interest_mismatch_predicate>`；仅当来源含语义确认的兴趣属性时
可用。

每群体输出 `user_key, 0 AS label, '<代号>' AS neg_pop`。`neg_pop` 仅统计用，
不进入投影表。step1_1 只 UNION plan 中实际启用的群体。
