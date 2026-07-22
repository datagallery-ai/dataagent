# step1_4: project_tables（全表投影）

<入口规则>两种模式均执行本步</入口规则>

**目的**：为 `projections[]` 中每张源表建 `step1_sampled_` 前缀交付表。列集与源表一致，唯一用户数 = `sampled_n`；用户表含 `label` 列（主路径 JOIN 追加 / prelabeled 保留源列）。

<必须>表数 = `projections[]` 项数 = `inventory_check.table_count`，少一张不得进入 step1_5</必须>。`step1_temp_*` 不参与计数。

## 前置

step1_3 已完成，`step1_temp_sampled_users` 可查；`read` `step1_0_table_schema.json` 与 `step1_0_sampling_plan.json`。<必须>从 plan 读取 `projections[]`（每条含 `table`、`type`）和 `inventory_check.table_count`，确定要建的表和张数</必须>。ENGINE 固定 `MergeTree() ORDER BY tuple()`。

---

## 表清单对齐（建表前最后核对）

查 ClickHouse 实表后与 `source_table_inventory.tables` 做差集：

```sql
SELECT name FROM system.tables
WHERE database = '{{source_database}}' AND name NOT LIKE 'step1_%'
ORDER BY name
```

- 差集为空 → 直接建表
- 漏表 → 语义补查结构与角色，更新 `step1_0_table_schema.json`（`tables[]` + `role_candidates`）和 plan（`source_table_inventory.tables` + `projections[]` + `inventory_check.table_count`）
- 语义有而库无 → 阻塞

---

## 投影模板

<必须>逐张建表，不得跳过。以下是要建的完整表清单：</必须>

- `projections[0]`：`<table_1>`（type: `<type_1>`）
- `projections[1]`：`<table_2>`（type: `<type_2>`）
- … 共 `inventory_check.table_count` 张

每张表：预检 → 建 `step1_sampled_<table>` → gate 验证，通过再建下一张。

> `<user_key_column>` = `projections[].user_key`，缺省用 `keys.user_key_default`；`<game_key_column>` = `keys.game_key_default`。

### user_table

<必须>按 `mode` 选择对应模板：</必须>

**`mode != "prelabeled"`**：JOIN `step1_temp_sampled_users` 追加 `label` 列。<必须>`SELECT DISTINCT src.*, s.label` 去重，防止源表 user_key 重复导致交付表膨胀。</必须>

```sql
CREATE OR REPLACE TABLE {{output_database}}.step1_sampled_<table>
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT DISTINCT src.*, s.label
FROM {{source_database}}.<table> AS src
INNER JOIN {{output_database}}.step1_temp_sampled_users AS s
  ON src.<user_key_column> = s.user_key
WHERE src.<user_key_column> IS NOT NULL AND src.<user_key_column> != '';
```

**`mode == "prelabeled"`**：<禁止>禁止 `SELECT src.*, s.label`（会重名列）</禁止>。<必须>`SELECT DISTINCT src.*` + `AND src.<keys.label_column> IN (0, 1)`</必须>。`<keys.label_column>` 类型：数值列 `IN (0, 1)`，String 列 `IN ('0', '1')`。<必须>`DISTINCT` 必须保留，防止源表 user_key 重复导致交付表膨胀。</必须>

```sql
CREATE OR REPLACE TABLE {{output_database}}.step1_sampled_<table>
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT DISTINCT src.*
FROM {{source_database}}.<table> AS src
INNER JOIN {{output_database}}.step1_temp_sampled_users AS s
  ON src.<user_key_column> = s.user_key
WHERE src.<user_key_column> IS NOT NULL AND src.<user_key_column> != ''
  AND src.<keys.label_column> IN (0, 1);
```

### user_keyed

含用户键的表，按采样用户过滤行。

```sql
CREATE OR REPLACE TABLE {{output_database}}.step1_sampled_<table>
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT *
FROM {{source_database}}.<table> AS t
WHERE t.<user_key_column> IS NOT NULL AND t.<user_key_column> != ''
  AND t.<user_key_column> IN (
    SELECT user_key FROM {{output_database}}.step1_temp_sampled_users
  );
```

### game_keyed

纯游戏维表，用 `sql_fragments.game_filter` 过滤相关游戏。

```sql
CREATE OR REPLACE TABLE {{output_database}}.step1_sampled_<table>
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT *
FROM {{source_database}}.<table> AS t
WHERE <sql_fragments.game_filter>
  AND t.<game_key_column> IS NOT NULL;
```

---

## Gate SQL（每张表建完后验证）

`user_table` / `user_keyed` 类型：

```sql
CREATE OR REPLACE TABLE {{output_database}}.step1_temp_step1_4_gate_<table>
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT
  uniqExact(<user_key_column>) AS out_users,
  (SELECT count() FROM {{output_database}}.step1_temp_sampled_users) AS sampled_n,
  (SELECT count() FROM {{output_database}}.step1_sampled_<table>) AS out_rows,
  (SELECT count() FROM {{source_database}}.<table>) AS src_rows;
```

`game_keyed` 类型只查行数：

```sql
CREATE OR REPLACE TABLE {{output_database}}.step1_temp_step1_4_gate_<table>
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT
  (SELECT count() FROM {{output_database}}.step1_sampled_<table>) AS out_rows,
  (SELECT count() FROM {{source_database}}.<table>) AS src_rows;
```

| table type | <必须>失败条件</必须> |
|---|---|
| `user_table` | `out_users != sampled_n` |
| `user_keyed` | `out_users > sampled_n` |
| `game_keyed` | 不做硬检查 |

---

## 建表完成检查

<必须>全部建完后执行此 SQL 验证表数：

```sql
SELECT count() AS actual
FROM system.tables
WHERE database = '{{output_database}}'
  AND name LIKE 'step1_sampled_%';
```

<必须>`actual == inventory_check.table_count`，不相等则回补缺失表</必须>，不得进入 step1_5。

---

## 产出

全部表建齐后进入 step1_5。不产出本地文件。
