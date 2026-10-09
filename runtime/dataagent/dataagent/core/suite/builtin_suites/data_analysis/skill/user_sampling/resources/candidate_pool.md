# 口径：candidate_pool

生成进入训练的候选用户集合。候选条件只使用 T0 及以前的信息：在
plan.`lookback_days` 内活跃，且截至 T0 未转化 `entity_scope.target`。

用户键、活动来源、转化来源、实体过滤和时间列只从 plan 的
`sampling_sources`、`keys`、`sql_fragments` 读取。模板是逻辑关系，提交前按
`logical_sql.md` 编译。

```text
WITH
active_users AS (
  SELECT DISTINCT <canonical_user_key_activity> AS user_key
  FROM {{source_database}}.<activity_source>
  WHERE <valid_user_key_predicate_activity>
    AND <pre_t0_lookback_predicate_activity>
),
converted_users AS (
  SELECT DISTINCT <canonical_user_key_conversion> AS user_key
  FROM {{source_database}}.<conversion_event>
  WHERE <valid_user_key_predicate_conversion>
    AND <entity_filter_predicate_conversion>
    AND <conversion_behavior_predicate>
    AND <through_t0_predicate_conversion>
)
SELECT a.user_key
FROM active_users AS a
<LOGICAL_ANTI_JOIN
  right="converted_users AS converted"
  on="a.user_key = converted.user_key">;
```
