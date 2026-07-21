# step1_0: 采样计划

**目的**：先落盘语义结果为 `step1_0_table_schema.json`，再落盘采样计划 `step1_0_sampling_plan.json`。

**全表投影**：<必须>`projections[]` 与源表一一对应，一张都不能少</必须>；step1_4 为每张源表建 `step1_sampled_<表名>`，全表投影。

---


## 本步做什么

1. 用语义服务查出目标库全部业务表的结构，写入 `step1_0_table_schema.json`。
2. 用 ClickHouse 查 `system.tables`，核对表清单是否和上一步一致；缺表则补查并写回 schema。
3. 用语义服务确认用户表、事件表、游戏维表及键列，写入 schema 的 `role_candidates`。
4. 根据 schema、任务参数和 mode，写出 `step1_0_sampling_plan.json`。
5. <重要>检查无误后：`mode=="prelabeled"` → step1_3；否则 → step1_1</重要>。
---

## plan 字段

> `database`/`run_id`/`T0`/`label_window_days`/`lookback_days`/`sample_size` 来自任务参数；`cold_start_threshold` 默认 500。  
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
  "database": "<string>",
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

<注意>`projections[].user_key` 与 `keys.user_key_default` 不同时显式写出</注意>


---

## 1. 数据schema落盘

分两次查：

**① 全库 schema（必做，只问清单与结构）**，query 固定为：

```text
列出数据库 <database> 中每一张业务表的完整结构：表名、全部列名与类型、主键、外键、表间 join 关系。要求覆盖该库全部业务表，逐表返回。
```

`read` tool-results 后用 **`write`** 写出 **`step1_0_table_schema.json`**（`tables[]` = 该库完整源表清单）。

**② 角色定位（必做，schema 齐备后）**：用语义服务确认用户表、事件表、游戏维表及键列，写入 schema 的 `role_candidates`（为 §3 写 plan 准备 `sampling_sources` / `keys`）。query 例如：

```text
在数据库 <database> 已列出的业务表中，分别指出用户表、付费/label 事件表、活跃行为表、游戏维度表，以及用户键、游戏键、事件时间列。目标游戏：<target_game>。
```

表不全时：按缺表再查语义，或 **一条** `SELECT ... FROM system.columns WHERE database = '...' AND table IN (...)` 批查后合并写回。本步结束后，下游只 `read` 该落盘文件取列信息。

### `step1_0_table_schema.json`

```json
{
  "database": "<目标库>",
  "query": "<semantic_retrieve 的 query>",
  "tables": [
    {
      "name": "<表名>",
      "description": "<可选>",
      "columns": [
        { "name": "<列名>", "valueType": "<STRING|FLOAT|...>", "isPrimaryKey": false }
      ]
    }
  ],
  "join_hints": [
    { "left": "<表.列>", "right": "<表.列>", "note": "<可选>" }
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

## 2. 与库核对

`read` schema 后提交：

```sql
SELECT name FROM system.tables
WHERE database = '<database>' AND name NOT LIKE 'step1_%'
ORDER BY name
```

- 集合一致 → 继续。
- 库多出的表 → 补语义或一条 `system.columns` 批查，**写回** schema。
- 语义有而库无 → 阻塞。

`source_table_inventory.tables` = 核对后的完整表名；每张表在 `projections[]` 占一行。

---

## 模式判定（§1② 语义确认用户表和键列后，§3 写 plan 前执行）

根据 schema 确认的 `user_table`、`user_key_default` 及 label 列名，提交 ClickHouse：

```sql
SELECT
  uniqExactIf(<sql_fragments.user_key_expr>, <label_column> = 1) AS pos_users,
  uniqExactIf(<sql_fragments.user_key_expr>, <label_column> = 0) AS neg_users
FROM <database>.<user_table>
WHERE <sql_fragments.valid_user>
```

- `pos_users > 0` 且 `neg_users > 0` → `mode="prelabeled"`：<必须>仅跳过 step1_1 / step1_2，其余步骤与主路径完全相同</必须>；写 plan 时走下面 §3.A 路径
- 否则 → 主路径，`mode="regular"`（step1_1 正样本 &lt; 500 则降级 `"cold_start"`）
- String 列用 `= '1'` / `'0'`，**禁止加 LIMIT 1**

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

`sampling_sources` / `keys` / `sql_fragments` 依据 schema 的 `tables` / `role_candidates` / `join_hints`。

---

### 3.1 sql_fragments 构造规则

每个片段必须写成**可直接拼入 ClickHouse WHERE / SELECT 的表达式**，不能留占位符或概念性描述。列名与类型取自 `step1_0_table_schema.json`。

<注意>键表达式：两种模式都需要</注意>

| 片段 | 规则 | 示例 |
|---|---|---|
| `user_key_expr` | 把用户键列包一层，确保非 NULL。字符串列用 `assumeNotNull(<col>)`，数值列直接用 `<col>` | `assumeNotNull(<user_key_default>)` |
| `valid_user` | 过滤用户键为 NULL 或空串的行 | `<user_key_default> IS NOT NULL AND <user_key_default> != ''` |
| `game_key_expr` | 同上，针对游戏键列 | `assumeNotNull(<game_key_default>)` |
| `game_filter` | 把目标游戏定位到一行或多行的条件。`game_scope.target` 是游戏名/ID，用键列匹配 | `<game_key_default> = '<game_scope.target>'` |

#### 时间片段（仅 regular 路径）

时间列的处理取决于 schema 中该列的 `valueType`：

| schema valueType | ClickHouse 写法 |
|---|---|
| `Date` / `DateTime` / `DateTime64` | `<col>` 直接参与比较 |
| `STRING` / `String` | `parseDateTimeBestEffortOrNull(<col>)` 包一层再比较 |

| 片段 | 窗口 | 写法 |
|---|---|---|
| `label_window` | `(T0, T0+N]` | `<time_col> > <T0> AND <time_col> <= <T0 + N>` |
| `pre_t0_lookback` | `(T0 - lookback_days, T0]` | `<time_col> > <T0 - lookback_days> AND <time_col> <= <T0>` |
| `through_t0` | `≤ T0` | `<time_col> <= <T0>` |

上述表达式中 `<time_col>` = `keys.event_time`，按上表的类型规则包裹。`T0`、`label_window_days`、`lookback_days` 来自任务参数。

**示例**：`label_window` = `<event_time>` 在 T0 到 T0+N 间；`pre_t0_lookback` = T0-L 到 T0；`through_t0` = ≤ T0。若列类型为 String 则包 `parseDateTimeBestEffortOrNull()`，若 Date/DateTime 则直接比较。

#### positive_label

`positive_label` 是标定正样本的 WHERE 条件（不含时间窗），用于 step1_1 probe 和 step1_3 的 `pos` CTE。

**取值画像（必做，写 plan 前）**：对 `y_label.event_table` 提一条 MCP `SELECT`，查看正样本相关的枚举列取值：

```sql
SELECT <enum_col>, count() AS cnt
FROM <database>.<y_label.event_table>
WHERE <valid_user> AND <game_filter> AND <label_window>
GROUP BY <enum_col>
ORDER BY cnt DESC
LIMIT 20
```

常见枚举列为 `entity_flag`、`status` 等（依据 `labels.md` 中该 family 的说明）。从结果中选取语义明确的取值写入 `positive_label`。**禁止**写库中未出现的取值。

示例：`<enum_col> = '<从画像结果中选取的取值>'`

---

## 4. 完成检查

<必须>step1_0_table_schema.json</必须> 已写；`tables[].name` 与 `system.tables` 一致（排除 `step1_%`）。  
<必须>step1_0_sampling_plan.json</必须> 已写；`source_table_inventory` / `projections` 1:1。  
<必须>`inventory_check.ok == true`</必须> 且 `table_count == len(projections)`。  
<必须>`projections[]` 每项 `type` 仅为 `user_table` / `user_keyed` / `game_keyed`</必须>。
