# 口径：candidate_pool

**目的**：生成进入训练的候选用户集合 `<user_id>`（后续据此削减各业务源表行内容）。候选条件仅使用 `T0` 及以前的信息（SKILL §4、§5）。

**逻辑角色**：用户键、游戏键、活动来源、事件时间列及目标转化口径（见 `resources/labels.md`）。活动来源由语义层从实际存在且具备用户键与时间列的事件流中选择，并记录为 `<activity_source>`。

**参数**：`{{source_database}}`、`game_scope`、`T0`；表名与 `sql_fragments` 来自 plan。

**表/列来源**：`sampling_sources` + `keys`；本模板只消费 plan 的 `sql_fragments`。

**规则**：`T0` 前 **90 天**内有任意游戏行为（活跃）**且** 截至 `T0` 未转化目标游戏/相似集合（排除已转化）。见 SKILL §5、§6。

独立探查时 `command` 须以 **`CREATE`** 或 **`SELECT`** 开头。嵌入 step1_3 建表语句时改写为 `pool AS (...)` 等 CTE，写在 `CREATE TABLE ... AS WITH` 之后。排除已转化用 `NOT IN` / `LEFT ANTI JOIN`（见 `resources/negative_samples.md`）。

```sql
SELECT a.user_key
FROM (
  SELECT DISTINCT <canonical_user_key_activity> AS user_key
  FROM {{source_database}}.<activity_source>
  WHERE <valid_user_key_predicate_activity>
    AND <pre_t0_lookback_predicate_activity>
) AS a
LEFT ANTI JOIN (
  SELECT DISTINCT <canonical_user_key_conversion> AS user_key
  FROM {{source_database}}.<目标转化所在事件表>
  WHERE <valid_user_key_predicate_conversion>
    AND <game_filter_predicate_conversion>
    AND <conversion_behavior_predicate>
    AND <through_t0_predicate_conversion>
) AS converted
  ON a.user_key = converted.user_key;
```
