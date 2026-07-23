# step1_5: stats

<入口规则>两种模式均执行本步</入口规则>

**目的**：统计 `output_database` 内与源表同名的投影表，写出 **`step1_output_meta.json`**（当前 job workspace）。

<必须>ClickHouse SQL 仅通过 `submit_resource_job`（`resource_id="clickhouse"`）执行。</必须>

## 前置

- step1_4 已完成，全部交付表（output_database 内与投影源表一一对应）在库中
- `read` `step1_0_table_schema.json` 与 `step1_0_sampling_plan.json`（含 `projections`）

---

## 1. 采样用户 / label 统计

找到 `projections[]` 中 `type == 'user_table'` 的那张表（唯一带 `label` 列的表），对其统计：

```sql
CREATE OR REPLACE TABLE {{output_database}}.step1_temp_stats_user_label
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT
  count() AS total_users,
  uniqExact(<user_key_column>) AS unique_users,
  countIf(label = 1) AS pos_cnt,
  countIf(label = 0) AS neg_cnt,
  pos_cnt * 1.0 / nullIf(neg_cnt, 0) AS pos_neg_ratio
FROM {{output_database}}.<user_table>;
```

`<user_key_column>` = `projections[].user_key`（缺省用 `keys.user_key_default`）。校验：
- `unique_users == total_users`（无重复用户）
- `pos_neg_ratio` 应接近 0.25（即 1:4 的硬约束）；远端偏离时自查原因

结果写入 `step1_output_meta.json` 的 `label_stats`。

---

## 2. 表数量核对

```sql
SELECT count() AS cnt
FROM system.tables
WHERE database = '{{output_database}}'
  AND name NOT LIKE 'step1_%';
```

- `expected` = `inventory_check.table_count`
- `actual` = 上条 SQL 的 `cnt`
- `ok` = 二者相等

缺失的交付表写入 `missing_tables`。`ok != true` → 回 step1_4 补建，**不得进入 step1_6**。

---

## 3. 各投影表行数

`projections[]` 中每张交付表分别物化一条 SQL：

```sql
CREATE OR REPLACE TABLE {{output_database}}.step1_temp_stats_rows_<table>
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT count() AS rows
FROM {{output_database}}.<table>;
```

写入 `projection_tables[]` 时：`table` 填完整表名，`type` 取自 `projections[].type`，`type == user_table` 时带 `has_label: true`。

---

## 4. 自查

写 `step1_output_meta.json` 前自行核对；异常则回对应步骤修复。检查项**不必**写入 `step1_output_meta.json`。

| 自查项 | 异常条件 |
|---|---|
| 比例 | `pos_neg_ratio ∉ [0.2, 0.3]`（偏离 1:4 硬约束的 20% 容差区间） |
| 去重 | `unique_users != total_users` |
| 空样本 | `pos_cnt == 0`（cold_start 下允许 > 0 但 < cold_start_threshold） |
| 缩行 | 某张 `user_keyed` 表 `rows / src_rows > 0.5`。`rows` 取自 §3 的 `step1_temp_stats_rows_*`；`src_rows` 取自 step1_4 gate 表 `step1_temp_step1_4_gate_<table>` 的 `src_rows` 列 |
| 表数 | `ok != true` → 直接回 step1_4，不走自查 |

---

## 5. step1_output_meta.json 结构

值来自 plan 与 §1～§3：

```json
{
  "run_id": "<plan.run_id>",
  "source_database": "<plan.source_database>",
  "output_database": "<plan.output_database>",
  "target_game": "<plan.game_scope.target>",
  "T0": "<plan.T0>",
  "label_window_days": "<plan.label_window_days>",
  "sample_size": "<plan.sample_size>",
  "actual_sample_size": "<label_stats.total_users>",
  "mode": "<plan.mode>",
  "table_count_check": {
    "expected": "<inventory_check.table_count>",
    "actual": "<§2 实库表张数>",
    "ok": true,
    "missing_tables": []
  },
  "label_stats": {
    "positive": "<§1 pos_cnt>",
    "negative": "<§1 neg_cnt>",
    "total": "<§1 total_users>",
    "pos_neg_ratio": "<§1 pos_neg_ratio>"
  },
  "projection_tables": [
    { "table": "<源表名>", "type": "user_table", "rows": "<§3>", "has_label": true },
    { "table": "<源表名>", "type": "user_keyed", "rows": "<§3>" },
    { "table": "<源表名>", "type": "game_keyed", "rows": "<§3>" }
  ]
}
```

必填字段：`run_id`、`source_database`、`output_database`、`target_game`、`T0`、`label_window_days`、`sample_size`、`actual_sample_size`、`mode`、`table_count_check`、`label_stats`、`projection_tables`。`mode` 为 `regular`、`cold_start` 或 `prelabeled`。`table` 为源表原名（output_database 内与源表同名）；`type: user_table` 须带 `has_label: true`。

---

## 产出

`step1_output_meta.json`（本地文件，写在当前 job workspace）。
