# 口径：labels（各 y-label 家族的正样本口径）

**目的**：给出每个 y-label 家族「窗口内正样本」的口径。所有家族共用：正样本 = `(T0, T0+N]` 窗口内、对结构化 `game_scope` 指定游戏、发生该家族目标行为的 canonical `user_key`。防泄漏见 SKILL §4：label 口径只用带事件时间的事件流。

**公共参数**：`{{database}}`、`game_scope`、`T0`、`label_window_days`；表名/列名/SQL 从 plan 的 `sampling_sources`、`keys`、`sql_fragments` 读取。

**类型适配**：时间/金额列类型以 **`step1_0_table_schema.json`** 为准，写入 plan 的 `sql_fragments`。

---

## 安装/下载

```sql
SELECT DISTINCT <canonical_user_key> AS user_key
FROM {{database}}.<behavior_event>
WHERE <valid_user_key_predicate>
  AND <game_filter_predicate>
  AND <install_open_predicate>
  AND <label_window_predicate>;
```

## 付费/收入（二分类）

```sql
SELECT DISTINCT <canonical_user_key> AS user_key
FROM {{database}}.<pay_booking_event>
WHERE <valid_user_key_predicate>
  AND <game_filter_predicate>
  AND <positive_pay_predicate>
  AND <label_window_predicate>;
```

## 付费/收入（回归）

```sql
SELECT <canonical_user_key> AS user_key, sum(<numeric_pay_amount_expr>) AS label_value
FROM {{database}}.<pay_booking_event>
WHERE <valid_user_key_predicate>
  AND <game_filter_predicate>
  AND <positive_pay_predicate>
  AND <label_window_predicate>
GROUP BY user_key;
```

## 预约

```sql
SELECT DISTINCT <canonical_user_key> AS user_key
FROM {{database}}.<pay_booking_event>
WHERE <valid_user_key_predicate>
  AND <game_filter_predicate>
  AND <booking_converted_predicate>
  AND <label_window_predicate>;
```

## 点击/CTR

```sql
SELECT DISTINCT <canonical_user_key> AS user_key
FROM {{database}}.<exposure_event>
WHERE <valid_user_key_predicate>
  AND <game_filter_predicate>
  AND <click_indicator_predicate>
  AND <label_window_predicate>;
```

## 曝光转化

```sql
SELECT DISTINCT <canonical_exposure_user_key> AS user_key
FROM {{database}}.<exposure_event> AS e
INNER JOIN {{database}}.<下游目标事件表> AS d
  ON <canonical_exposure_user_key> = <canonical_downstream_user_key>
 AND <canonical_exposure_game_key> = <canonical_downstream_game_key>
WHERE <valid_join_key_predicates>
  AND <game_filter_predicate_exposure>
  AND <label_window_predicate_exposure>
  AND <downstream_positive_predicate>
  AND <downstream_after_exposure_predicate>
  AND <label_window_predicate_downstream>;
```

## 留存/活跃

```sql
SELECT <canonical_user_key> AS user_key
FROM {{database}}.<behavior_event>
WHERE <valid_user_key_predicate>
  AND <game_filter_predicate>
  AND <label_window_predicate>
GROUP BY user_key
HAVING uniqExact(<canonical_active_day_expr>) >= {{active_threshold}};
```

## 时长/参与度

二分类：
```sql
SELECT <canonical_user_key> AS user_key
FROM {{database}}.<behavior_event>
WHERE <valid_user_key_predicate>
  AND <game_filter_predicate>
  AND <label_window_predicate>
  AND <valid_duration_predicate>
GROUP BY user_key
HAVING sum(<numeric_duration_expr>) >= {{duration_threshold}};
```

回归口径为 `SELECT <canonical_user_key> AS user_key, sum(<numeric_duration_expr>) AS label_value ... GROUP BY user_key`，无 `HAVING`。

## 搜索/浏览意向

```sql
SELECT DISTINCT <canonical_user_key> AS user_key
FROM {{database}}.<behavior_event>
WHERE <valid_user_key_predicate>
  AND <game_filter_predicate>
  AND <search_browse_predicate>
  AND <label_window_predicate>;
```

---

**取值词表**：安装、预约等取值须在 **`semantic_retrieve`** 返回中确认，写入 plan 的 `sql_fragments`。

**输出**：一列 canonical `user_key`（回归再带 `label_value`），供 `scripts/step1_3_build_training_set.md` 的 `pos` 使用。
