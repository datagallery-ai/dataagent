# step1_0: 采样计划

**目的**：先落盘语义结果为 `step1_0_table_schema.json`，再落盘采样计划 `step1_0_sampling_plan.json`。

**全表投影**：<必须>`projections[]` 与源表一一对应，一张都不能少</必须>；step1_4 为每张源表在 output_database 建同名表，全表投影。

---


## 本步做什么

1. **ClickHouse 取全局表名列表**：`SELECT name FROM system.tables WHERE database = '{{source_database}}'`，得到 `source_table_inventory.tables`（<必须>这是唯一权威的完整表清单</必须>）。
2. **三步语义查库**（①②③，每次 query 后与 ClickHouse 全局列表做差集，遗漏表逐批补查，直到全覆盖）：
   - ① 全库表+列结构（表名、列名、类型、描述、主键）
   - ② 表间 JOIN 关系
   - ③ 角色定位（用户表、事件表、游戏维表及键列）
3. <必须>step1_0_table_schema.json 中 `tables[].name` 与 ClickHouse 全局表名列表 1:1 一致后，方可落地</必须>。
4. 根据 schema、任务参数和 mode，写出 `step1_0_sampling_plan.json`。
5. <重要>检查无误后：`mode=="prelabeled"` → step1_3；否则 → step1_1</重要>。
---

## plan 字段

> `run_id`/`T0`/`label_window_days`/`lookback_days`/`sample_size` 来自任务参数；`cold_start_threshold` 默认 500。  
> `source_database` 存放原始业务表（只读），`output_database` 存放 step1 产物表。  
> `mode`：`"regular"` / `"cold_start"` / `"prelabeled"`。  
> `game_scope`：`target` 目标游戏名，`similar_games` 由 step1_2 填写。  
> `y_label`：`family` 对应 `labels.md` 家族名，`task_type` 固定 `"binary_classification"`。  
> `sampling_sources`：语义服务确认的角色→表名，`game_dim` 是游戏维度表。  
> `keys`：`similar_dim` 是游戏维表中匹配相似游戏的维度列（如 game_type）。  
> `sql_fragments`：键表达式固定规则见 §3.1；时间/正样本片段仅 regular 路径填写。  
> `negative_populations`：`neg_k` 仅作参考，实际负样本量由 `pos × 4` 计算。  
> `projections[]`：与源表一一对应，`type` 为 `user_table` / `user_keyed` / `game_keyed`。

```json
{
  "source_database": "<源库>",
  "output_database": "<产物库>",
  "run_id": "<string>",
  "T0": "<ISO日期>",
  "label_window_days": "<number>",
  "lookback_days": "<number>",
  "sample_size": "<number>",
  "cold_start_threshold": 500,
  "mode": "regular",
  "game_scope": { "target": "<string>", "similar_games": [] },
  "y_label": { "family": "<string>", "task_type": "binary_classification", "event_table": "<string>" },
  "sampling_sources": {
    "user_table": "<string>",
    "label_event": "<string>",
    "activity_event": "<string>",
    "conversion_event": "<string>",
    "game_dim": "<string>"
  },
  "keys": {
    "user_key_default": "<string>",
    "user_key_behavior": "<string>",
    "game_key_default": "<string>",
    "event_time": "<string>",
    "similar_dim": "<string>",
    "label_column": null
  },
  "sql_fragments": {
    "user_key_expr": "<string>",
    "valid_user": "<string>",
    "game_key_expr": "<string>",
    "game_filter": "<string>",
    "label_window": "<string>",
    "positive_label": "<string>",
    "pre_t0_lookback": "<string>",
    "through_t0": "<string>"
  },
  "negative_populations": [{ "code": "<string>", "neg_k": 0, "description": "<string>" }],
  "source_table_inventory": { "tables": ["<table1>", "<table2>"] },
  "inventory_check": { "ok": true, "table_count": "<number>" },
  "projections": [
    { "table": "<string>", "type": "user_table", "user_key": "<string>" },
    { "table": "<string>", "type": "user_keyed", "user_key": "<string>" },
    { "table": "<string>", "type": "game_keyed" }
  ]
}
```

**prelabeled 填法**：`mode` 写 `"prelabeled"`；`y_label.event_table` / `sampling_sources.label_event` / `sampling_sources.activity_event` / `sampling_sources.conversion_event` / `sql_fragments`（除 `user_key_expr`、`valid_user`、`game_filter` 外）写 `null`；`negative_populations` 写 `[]`；`keys.label_column` 必填（用户表实列名）。`sql_fragments.game_filter` 仅当 `projections[]` 有 `game_keyed` 时才构造。


---

## 1. 数据schema落盘

### 0. 全局表名列表（最先做，唯一基准）

先用 ClickHouse 查出 `source_database` 里所有业务表名，写入 `source_table_inventory.tables`：

```sql
SELECT name FROM system.tables
WHERE database = '{{source_database}}'
ORDER BY name
```

`source_table_inventory.tables` 就是后续三步语义补全时需要匹配的全集。`inventory_check.table_count` = 该列表长度。

---

### 三道语义查询 + 逐批补查（统一规则）

<必须>每道语义 query 返回后立即取 `table_names` 与 ClickHouse 全局表名列表做差集</必须>：语义未覆盖的表需补查，直至全覆盖才进入下一道。

---

**① 全库表+列结构**

query 固定为：

```text
数据库 <source_database> 中每一张业务表，逐表列出：
- 表名
- 表用途描述
- 全部列名
- 每列的数据类型（String/Int64/Float64/Date/DateTime 等）
- 每列的业务含义描述
- 哪些列是主键
要求覆盖该库全部业务表，每张表的每一列都必须列出，不可省略。
```

`read` → 差集 → 补查 → 循环，直到全局列表中每张表都出现在返回结果里。写入 schema 的 `tables[]`。

**② 表间 JOIN 关系**

query 固定为：

```text
数据库 <source_database> 中所有业务表之间的 JOIN 关系，逐对列出：
- 左表.列 → 右表.列
- JOIN 的业务含义（如"通过 user_id 关联用户信息"）
要求覆盖该库所有可能的表间关联。
```

`read` → 差集（join_hints 中未出现过的表） → 补查，直到全局列表中每张表都参与至少一条 JOIN 关系（或确认该表确实无关联）。写入 schema 的 `join_hints[]`。

**③ 角色定位**

query 固定为：

```text
在数据库 <source_database> 已列出的业务表中，分别指出：
- 用户信息表（含用户画像/属性）
- 付费/label 转化事件表
- 活跃行为事件表
- 游戏维度表
并指出每张表中的用户键列、游戏键列、事件时间列。
目标游戏：<target_game>。
```

`read` → 差集（role_candidates 所有角色中均未出现的表） → 补查，直到全局列表中每张表都被分配到一个角色（无法归入五类的，允许在 role_candidates 中留空或不出现，但必须显式确认过）。写入 schema 的 `role_candidates`。

#### 兜底补查（①②③ 通用）

每轮语义查询返回后，提取返回结果中出现的表名，与 ClickHouse 全局列表（`source_table_inventory.tables`）做集合减：`未覆盖表 = 全局列表 \ 返回表`。未覆盖的表补查时在原 query 末尾显式列出遗漏表名，例如："以下表还未获取信息，请补充：`table_a`、`table_b`…"。补查仍无效时，允许用 ClickHouse 的 `DESCRIBE TABLE` 作为列结构的兜底。JOIN 和角色无法兜底时记为"未覆盖"，不阻塞流程。

---

### 落盘

三道全部补完后，合并写出 **`step1_0_table_schema.json`**（`tables` 来自 ①，`join_hints` 来自 ②，`role_candidates` 来自 ③）。

<必须>`table_names` 与 `source_table_inventory.tables` 1:1 一致后方可落盘</必须>。

### `step1_0_table_schema.json`

```json
{
  "source_database": "<source_database>",
  "table_names": ["<表名1>", "<表名2>", "…"],
  "tables": [
    {
      "name": "<表名>",
      "description": "<表用途描述>",
      "columns": [
        { "name": "<列名>", "valueType": "<STRING|Int64|Float64|Date|DateTime|...>", "description": "<列的业务含义>", "isPrimaryKey": false }
      ]
    }
  ],
  "join_hints": [
    { "left": "<表.列>", "right": "<表.列>", "note": "<JOIN 业务含义>" }
  ],
  "role_candidates": {
    "user_table": ["<候选表>"],
    "label_event": ["<候选表>"],
    "activity_event": ["<候选表>"],
    "conversion_event": ["<候选表>"],
    "game_dim": ["<候选表>"]
  }
}
```

---

### 键表达式预构造（模式判定前，仅从 schema 构造）

从 schema 中提取用户键列的列名和类型，按 §3.1 规则构造 `user_key_expr` 和 `valid_user` 两条片段供模式判定 SQL 使用。（`game_key_expr` 和 `game_filter` 此时暂不需要。）

## 2. 判定 mode：查用户表是否已有 label

在写 plan 之前，先判定是 `prelabeled` 还是 `regular`：

- **prelabeled**：用户表里已经自带 label 列（0/1 两侧都有数据），跳过 step1_1/step1_2
- **regular**：用户表没有 label 列，需要走事件口径判定正负样本

**前置条件**：schema 已就绪，`user_key_expr` 和 `valid_user` 已预构造。

执行下方 SQL（`<label_pos_val>`/`<label_neg_val>` 按 label 列类型填：Int* → `1`/`0`，String → `'1'`/`'0'`）：

```sql
SELECT
  uniqExactIf(<user_key_expr>, <label_column> = <label_pos_val>) AS pos_users,
  uniqExactIf(<user_key_expr>, <label_column> = <label_neg_val>) AS neg_users
FROM {{source_database}}.<user_table>
WHERE <valid_user>
```

结果判定：
- `pos_users > 0` 且 `neg_users > 0` → `mode="prelabeled"`，按下方 §3.A 写 plan
- 否则 → `mode="regular"`，按下方 §3.B 写 plan（step1_1 正样本 < 500 时降级为 `cold_start`）

**禁止加 LIMIT 1**。

## 3. 填写 plan

在 schema 与库表集合一致后：`read` schema + 任务参数 → 按 mode 选择对应路径，`write`/`edit` `step1_0_sampling_plan.json`。

**以下路径二选一：**

---

### 3.A prelabeled 路径（`mode=="prelabeled"` 时走这里）

按上方 plan JSON 的 <必须>prelabeled 填法</必须> 逐字段填写。补充约束：

- <必须>`keys` 额外必填 `label_column`</必须>（用户表已有 label 的列名，以 schema 实列为准）
- <必须>仅当 `projections[]` 中有 `game_keyed` 时才构造 `sql_fragments.game_filter`</必须>（规则见 §3.1 键表达式）
- <禁止>对事件表发起 MCP `SELECT` 枚举画像</禁止>
- <禁止>把事件表写入 `sampling_sources`</禁止>

`user_key_expr` / `valid_user` / `game_filter` 构造规则见 §3.1 键表达式。

---

### 3.B regular 路径（`mode!="prelabeled"` 时走这里）

**y-label 家族映射**：根据 objective 选择对应的 label 家族：

| 家族 | 关键词 |
|---|---|
| 安装/下载 | 下载、安装、拉新 |
| 付费/收入 | 付费、ARPU |
| 预约 | 预约 |
| 点击/CTR | 点击、CTR |
| 留存/活跃 | 留存、DAU |
| 时长/参与 | 时长、参与 |

负样本默认：付费 **N4**；CTR **N2**；安装/留存/时长 **N3+N2**；至少一 hard + **N5**。

**填写顺序**：顶层参数 → `game_scope` → `y_label` → `sampling_sources` → `keys` → `sql_fragments` → `negative_populations` → `source_table_inventory` → `projections[]` → `inventory_check`。

`sampling_sources` / `keys` / `sql_fragments` 依据 schema 的 `role_candidates` / `join_hints`。

---

### 3.1 sql_fragments 构造规则

每个片段必须写成**可直接拼入 ClickHouse WHERE / SELECT 的表达式**，不能留占位符或概念性描述。列名与类型取自 `step1_0_table_schema.json`。

| 片段 | 规则 | 示例 |
|---|---|---|
| `user_key_expr` | 用户键非 NULL：String 列 `assumeNotNull(<col>)`，Int/数值列直接用 `<col>` | `assumeNotNull(user_id)` |
| `valid_user` | 过滤用户键为 NULL（String 列加 `!= ''`） | `user_id IS NOT NULL AND user_id != ''` |
| `game_key_expr` | 同上，针对游戏键列 | `assumeNotNull(game_id)` |
| `game_filter` | 定位目标游戏。String 键列 → `= '<game_scope.target>'`，Int* → `= <game_scope.target>` | `game_id = 'genshin'` |

#### 时间片段（仅 regular 路径）

| 片段 | 窗口 | 写法（`<tc>` 为 `keys.event_time`，String 列包 `parseDateTimeBestEffortOrNull(<tc>)`，Date/DateTime 直接写） |
|---|---|---|
| `label_window` | `(T0, T0+N]` | `<tc> > <T0> AND <tc> <= <T0 + N>` |
| `pre_t0_lookback` | `(T0 - L, T0]` | `<tc> > <T0 - L> AND <tc> <= <T0>` |
| `through_t0` | `≤ T0` | `<tc> <= <T0>` |

式中 `<T0>` = `T0`、`<N>` = `label_window_days`、`<L>` = `lookback_days`。

#### positive_label

`positive_label` 是标定正样本的 WHERE 条件（不含时间窗），用于 step1_1 probe 和 step1_3 的 `pos` CTE。

**取值画像（必做，写 plan 前）**：对 `y_label.event_table` 提一条 MCP `SELECT`，查看正样本相关的枚举列取值：

```sql
SELECT <enum_col>, count() AS cnt
FROM {{source_database}}.<y_label.event_table>
WHERE <valid_user> AND <game_filter> AND <label_window>
GROUP BY <enum_col>
ORDER BY cnt DESC
LIMIT 20
```

常见枚举列为 `entity_flag`、`status` 等（依据 `labels.md` 中该 family 的说明）。从结果中选取语义明确的取值写入 `positive_label`。**禁止**写库中未出现的取值。

示例：`<enum_col> = '<从画像结果中选取的取值>'`

---

## 4. 完成检查

<必须>step1_0_table_schema.json</必须> 已写；`table_names` 与 `source_table_inventory.tables` 1:1 一致。  
<必须>step1_0_sampling_plan.json</必须> 已写；`source_table_inventory` / `projections` 1:1。  
<必须>`inventory_check.ok == true`</必须> 且 `table_count == len(projections)`。  
<必须>`projections[]` 每项 `type` 仅为 `user_table` / `user_keyed` / `game_keyed`</必须>。
