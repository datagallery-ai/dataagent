# step1_4: project_tables（全表投影）

**目的**：在源表所在的 `<database>` 内，为 `projections[]` 中的每一张源表建 `step1_sampled_` 前缀交付表。列集与源表一致，行数缩到采样用户范围；用户表含 `label` 列（主路径追加 / prelabeled 保留源列）。

**硬约束**：建成的 `step1_sampled_*` 表张数 = `projections[]` 项数 = `inventory_check.table_count`；少一张交付表就不能算完成，也禁止进入 step1_5。`step1_temp_*` 不参与计数。

## 前置

- step1_3 已完成，`step1_temp_sampled_users(user_key, label)` 可查。主路径下 label 由 step1_3 事件口径计算；prelabeled 下 label 来自源表已有列。
- `step1_0_table_schema.json` 与 `step1_0_sampling_plan.json` 已读
- `projections[]` 每项有 `table`、`type`、`user_key`（缺省用 `keys.user_key_default`）。

三个 SQL 模板中 ENGINE 已固定为 `MergeTree() ORDER BY tuple()`，无需变更。

---

## 表清单最终对齐（建表前必做）

step1_0 的语义检索可能漏表（同一库多次查询返回结果不一致），本步在逐张建表前做最后一次核对，确保 `projections[]` 覆盖库中所有业务表。

**1. 查 ClickHouse 实表清单：**

```sql
SELECT name FROM system.tables
WHERE database = '<database>' AND name NOT LIKE 'step1_%'
ORDER BY name
```

**2. 与 plan 做差集：**

- 读取 `source_table_inventory.tables`（plan 中已登记的表）
- `CH 实表集合 \ plan 已登记集合` = **漏表列表**
- `plan 已登记集合 \ CH 实表集合` = **报错**（源表被删，阻塞）

**3. 对漏表补全：**

漏表非空时，逐张用语义服务补查（一次性问清结构与角色）：

```text
数据库 <database> 中新增业务表 <漏表名>。请返回：
1. 列名、类型、主键
2. 与现有表的 join 关系
3. 该表是否含用户键列？如有，列名是什么？
4. 该表是否含游戏键列？如有，列名是什么？
5. 该表的角色：用户表（含用户基础信息）、事件表（含用户键 + 事件时间）、游戏维表（含游戏键 + 游戏属性）
```

补全后按以下顺序更新文件：

a. **`step1_0_table_schema.json`**：在 `tables[]` 末尾追加该表的结构（`name`、`columns[]`）；新增 join 关系追加到 `join_hints`；将该表加入 `role_candidates` 对应数组（事件表 → `label_event` / `activity_event`，视业务含义而定）。

b. **`step1_0_sampling_plan.json`**：
   - `source_table_inventory.tables` 中追加漏表名
   - `projections[]` 中追加对应条目：
     - 用户表 → `type: "user_table"`，补 `user_key`
     - 事件表 → `type: "user_keyed"`，补 `user_key`
     - 游戏维表 → `type: "game_keyed"`
   - 更新 `inventory_check.table_count` = `len(source_table_inventory.tables)`

**4. 对齐完成：**

`projections[]` 长度 = `source_table_inventory.tables` 长度 = CH 实表数 → 重新 `read` 更新后的 plan → 进入下方逐张建表流程。

---

## 投影类型与 SQL 模板

`projections[]` 有三种 `type`，决定该表的裁剪方式。**每张表依次**：预检 → 建 `step1_sampled_<表名>` → gate 验证，通过再建下一张。

> `<user_key_column>` = `projections[].user_key`（缺省用 `keys.user_key_default`）。下方模板中的 `<user_key_column>` 均以此规则替换。

### 2.1 user_table（带 label）

**模式分支**：`read` plan 的 `mode` 字段，选对应模板。

#### 主路径（mode ≠ prelabeled）

用户表 JOIN `step1_temp_sampled_users`，追加 `label` 列。

```sql
CREATE OR REPLACE TABLE <database>.step1_sampled_<table>
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT src.*, s.label
FROM <database>.<table> AS src
INNER JOIN <database>.step1_temp_sampled_users AS s
  ON src.<user_key_column> = s.user_key
WHERE src.<user_key_column> IS NOT NULL AND src.<user_key_column> != '';
```

#### prelabeled 分支

源表已有 `label` 列，**只裁行不追加列**。<禁止>禁止 `SELECT src.*, s.label`</禁止>（会重名列）。<必须>`AND src.<keys.label_column> IN (0, 1)`</必须>确保只保留 label=0 或 1 的行，排除同一用户该列非 0/1 的其他行。`<keys.label_column>` 类型决定写法：数值列用 `IN (0, 1)`，String 列用 `IN ('0', '1')`。

```sql
CREATE OR REPLACE TABLE <database>.step1_sampled_<table>
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT src.*
FROM <database>.<table> AS src
INNER JOIN <database>.step1_temp_sampled_users AS s
  ON src.<user_key_column> = s.user_key
WHERE src.<user_key_column> IS NOT NULL AND src.<user_key_column> != ''
  AND src.<keys.label_column> IN (0, 1);
```

### 2.2 user_keyed

含用户键的表，按采样用户过滤行。含用户键的双键表也归此类。

```sql
CREATE OR REPLACE TABLE <database>.step1_sampled_<table>
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT *
FROM <database>.<table> AS t
WHERE t.<user_key_column> IS NOT NULL AND t.<user_key_column> != ''
  AND t.<user_key_column> IN (
    SELECT user_key FROM <database>.step1_temp_sampled_users
  );
```

### 2.3 game_keyed

纯游戏维表，没有用户键，用 `sql_fragments.game_filter` 过滤相关游戏。

```sql
CREATE OR REPLACE TABLE <database>.step1_sampled_<table>
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT *
FROM <database>.<table> AS t
WHERE <sql_fragments.game_filter>
  AND t.<game_key_column> IS NOT NULL;
```

> `<game_key_column>`：该表游戏键列名，取 `keys.game_key_default`。`sql_fragments.game_filter` 直接代入，它是对游戏键列的过滤条件。

---

## Gate SQL（每张表建完后验证）

`user_table` 和 `user_keyed` 类型执行：

```sql
CREATE OR REPLACE TABLE <database>.step1_temp_step1_4_gate_<table>
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT
  uniqExact(<user_key_column>) AS out_users,
  (SELECT count() FROM <database>.step1_temp_sampled_users) AS sampled_n,
  (SELECT count() FROM <database>.step1_sampled_<table>) AS out_rows,
  (SELECT count() FROM <database>.<table>) AS src_rows;
```

`game_keyed` 类型只查行数（无用户键列，不统计 `out_users`）：

```sql
CREATE OR REPLACE TABLE <database>.step1_temp_step1_4_gate_<table>
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT
  (SELECT count() FROM <database>.step1_sampled_<table>) AS out_rows,
  (SELECT count() FROM <database>.<table>) AS src_rows;
```

判断：

| table type | 失败条件 |
|---|---|
| `user_table` | `out_users != sampled_n`（必须覆盖且不超采样用户） |
| `user_keyed` | `out_users > sampled_n`（IN 裁剪后唯一用户数不应超过 sampled_n） |
| `game_keyed` | 允许较高 keep_ratio，不做 FAIL 硬检查 |

---

## 产出

为 `projections[]` 中每项建一张 ClickHouse 交付表：

| 表名 | 说明 |
|---|---|
| `<database>.step1_sampled_<源表名>` | 列集 = 源表列集（主路径 user_table 型多一列 `label`），行数缩到采样范围 |

不产出本地文件。全部表建齐后进入 step1_5。
