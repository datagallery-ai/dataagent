# step1_0: 采样计划

**目的**：先落盘语义结果为 `step1_0_table_schema.json`，再落盘采样计划 `step1_0_sampling_plan.json`。

**全表投影**：`projections[]` 里的内容与源表一一对应；step1_4 为每张源表建 `step1_sampled_<表名>`，全表投影，不能跳过。

---

## 模式判定（先判，再执行 §1~§4）

| 条件 | mode | 后续影响 |
|---|---|---|
| 用户表**已有**可用 `label` 列 | `"prelabeled"` | 跳过 step1_1 / step1_2；step1_3 走 prelabeled 分支；plan 只需预标注相关字段（见 §3 prelabeled 小节） |
| 否则 | `"regular"` 或 `"cold_start"` | 完整主路径 |

**无论哪种模式，语义检索（§1①+②）都不跳过**（prelabeled 与主路径都需要完整的表结构与角色定位）。prelabeled 唯一不同是 §3 写 plan 时的字段取舍。

**判定实施**（§1② 确认用户表和键列后执行，§3 写 plan 前完成）：根据 schema 确认的 `user_table`、`user_key_default`、label 列名，提交一条 ClickHouse：

```sql
SELECT
  uniqExactIf(<sql_fragments.user_key_expr>, <label_column> = 1) AS pos_users,
  uniqExactIf(<sql_fragments.user_key_expr>, <label_column> = 0) AS neg_users
FROM <database>.<user_table>
WHERE <sql_fragments.valid_user>
```

- `pos_users > 0` 且 `neg_users > 0` → `mode="prelabeled"` → §3 写 plan 时走 §3.pre
- 否则 → 主路径 `mode="regular"`（或 step1_1 因正样本少而改写 `cold_start`）
- String 列用 `= '1'` / `'0'`，**禁止加 LIMIT 1**

---

## 本步做什么（做完再执行 step1_1 或 step1_3）

1. 用语义服务查出目标库全部业务表的结构，写入 `step1_0_table_schema.json`。
2. 用 ClickHouse 查 `system.tables`，核对表清单是否和上一步一致；缺表则补查并写回 schema。
3. 用语义服务确认用户表、事件表、游戏维表及键列，写入 schema 的 `role_candidates`，并据此填写 plan 的 `sampling_sources` / `keys`。
4. 根据 schema 和任务参数，写出 `step1_0_sampling_plan.json`。
5. 检查无误后（表清单齐、plan 字段齐），再进入下一步。

---

## plan 字段

| 字段 | 说明 | prelabeled |
|---|---|---|
| 运行参数 | `database`、`run_id`、`T0`、`label_window_days`、`lookback_days`、`sample_size`（必填，人数）、`cold_start_threshold`（默认 500） | 同主路径 |
| `mode` | `"regular"` / `"cold_start"` / `"prelabeled"`；**prelabeled 由 §模式判定写入，step1_1 不再更新** | `"prelabeled"`（在 step1_0 即写入） |
| `game_scope` | `{ target, similar_games }`；`similar_games` 由 step1_2 填写 | `similar_games: []`（跳过 step1_2） |
| `y_label` | `{ family, task_type, event_table }`；`family` 对应 `labels.md` 中的家族名 | 必填 `family` + `task_type("binary_classification")`；`event_table` 写 `null` |
| `sampling_sources` | 逻辑角色 → 表名：`user_table`、`label_event`、`activity_event`、`conversion_event`、`game_dim` | **只须 `user_table`**；其余 role 写 `null` |
| `keys` | 共享列名：`user_key_default`、`user_key_behavior`、`game_key_default`、`event_time`、`similar_dim`（游戏维表中用于匹配相似游戏的维度列，如 game_type） | 额外必填 `label_column`（用户表已有 label 的列名）；`event_time` 可写 `null` |
| `sql_fragments` | 共享 SQL 片段：`user_key_expr`、`valid_user`、`game_key_expr`、`game_filter`、`label_window`、`positive_label`、`pre_t0_lookback`、`through_t0` | **只须填** `user_key_expr`、`valid_user`、`game_filter`（有 `game_keyed` 投影时）；其余片段写 `null`；禁止对事件表发起 MCP 枚举画像 |
| `negative_populations` | 数组，每项 `{ code, neg_k, description }` | `[]` |
| `source_table_inventory` | `{ tables: [...] }`，全部源业务表名 | 同主路径 |
| `inventory_check` | `{ ok, table_count }`；`table_count` 等于源表数 | 同主路径 |
| `projections[]` | 与源表一一对应，每项 `{ table, type, user_key? }`；`type` 为 `user_table` / `user_keyed` / `game_keyed` | 同主路径；用户键列确认见 §3 prelabeled 小节 |

- 如果某张表的用户键与 `keys.user_key_default` 不同，在 `projections[].user_key` 中写明（例如 `game_behavior_*` 系列用 `rank_flg`）。
- 每个字段的数据类型不必在 plan 里重复写，直接查 `step1_0_table_schema.json`。


---

## 1. 数据schema落盘

分两次查：

**① 全库 schema（必做，只问清单与结构）**，query 固定为：

```text
列出数据库 <database> 中每一张业务表的完整结构：表名、全部列名与类型、主键、外键、表间 join 关系。要求覆盖该库全部业务表，逐表返回。
```

`read` tool-results 后用 **`write`** 写出 **`step1_0_table_schema.json`**（`tables[]` = 该库完整源表清单）。

**② 角色定位（必做，schema 齐备后）**：用语义服务确认用户表、事件表、游戏维表及键列，写入 schema 的 `role_candidates`，并据此填写 plan 的 `sampling_sources` / `keys`。query 例如：

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

- 集合一致 → §3。
- 库多出的表 → 补语义或一条 `system.columns` 批查，**写回** schema。
- 语义有而库无 → 阻塞。

`source_table_inventory.tables` = 核对后的完整表名；每张表在 `projections[]` 占一行。

---

### y-label 家族映射

根据 objective 选择对应的 label 家族：

| 家族 | 关键词 |
|---|---|
| 安装/下载 | 下载、安装、拉新 |
| 付费/收入 | 付费、ARPU |
| 预约 | 预约 |
| 点击/CTR | 点击、CTR |
| 留存/活跃 | 留存、DAU |
| 时长/参与 | 时长、参与 |

负样本默认：付费 **N4**；CTR **N2**；安装/留存/时长 **N3+N2**；至少一 hard + **N5**。

---

## 3. 填写 plan

在 schema 与库表集合一致后：`read` schema + 任务参数 → `write`/`edit` `step1_0_sampling_plan.json`。

**若 `mode=="prelabeled"`**：先跳到 [§3.pre 预标注模式填写](#3pre-预标注模式填写-modespan-classequalsprelabeledtrueprelabeledspan-时必读)，填完后直接去 §4 示例和 §5 完成检查，**跳过 y-label 家族映射 和 §3.1 positive_label 画像**。`user_key_expr` / `valid_user` / `game_filter` 的构造规则仍然参考 §3.1 键表达式和时间片段。

**否则（主路径）**：按以下 y-label 家族映射选取 family，再按 §3.1 逐片段构造。

顺序：顶层参数 → `game_scope` → `y_label` → `sampling_sources` → `keys` → `sql_fragments` → `negative_populations` → `source_table_inventory` → `projections[]` → `inventory_check`。

`sampling_sources` / `keys` / `sql_fragments` 依据 schema 的 `tables` / `role_candidates` / `join_hints`。

---

### 3.1 sql_fragments 构造规则

每个片段必须写成**可直接拼入 ClickHouse WHERE / SELECT 的表达式**，不能留占位符或概念性描述。列名与类型取自 `step1_0_table_schema.json`。

#### 键表达式

| 片段 | 规则 | 示例 |
|---|---|---|
| `user_key_expr` | 把用户键列包一层，确保非 NULL。字符串列用 `assumeNotNull(<col>)`，数值列直接用 `<col>` | `assumeNotNull(<user_key_default>)` |
| `valid_user` | 过滤用户键为 NULL 或空串的行 | `<user_key_default> IS NOT NULL AND <user_key_default> != ''` |
| `game_key_expr` | 同上，针对游戏键列 | `assumeNotNull(<game_key_default>)` |
| `game_filter` | 把目标游戏定位到一行或多行的条件。`game_scope.target` 是游戏名/ID，用键列匹配 | `<game_key_default> = '<game_scope.target>'` |

#### 时间片段

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

**示例**（schema 中 `event_time` 列为 STRING 类型，`label_window_days = N`，`lookback_days = L`）：

```
label_window:    parseDateTimeBestEffortOrNull(<event_time>) >  parseDateTimeBestEffortOrNull('<T0>')
             AND parseDateTimeBestEffortOrNull(<event_time>) <= parseDateTimeBestEffortOrNull('<T0+N>')

pre_t0_lookback: parseDateTimeBestEffortOrNull(<event_time>) >  parseDateTimeBestEffortOrNull('<T0-L>')
             AND parseDateTimeBestEffortOrNull(<event_time>) <= parseDateTimeBestEffortOrNull('<T0>')

through_t0:      parseDateTimeBestEffortOrNull(<event_time>) <= parseDateTimeBestEffortOrNull('<T0>')
```

若 schema 中列为 Date/DateTime 类型，去掉 `parseDateTimeBestEffortOrNull()`，直接用列名比较。

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

### 3.pre 预标注模式填写（`mode=="prelabeled"` 时必读，读完跳至 §4）

**按上方 plan 字段表的 prelabeled 列逐字段填写**。字段表中已明确每项取值（`mode` 写 `"prelabeled"`、`negative_populations` 写 `[]`、大部分 `sql_fragments` 写 `null` 等），不再重复列。

额外注意：

- `keys` 额外必填 `label_column`（用户表已有 label 的列名，以 schema 实列为准）
- `sql_fragments.game_filter` 仅当 `projections[]` 中有 `game_keyed` 时才构造（按 §3.1 键表达式规则）

**禁止的**：
- <禁止>对事件表发起 MCP `SELECT` 枚举画像（不需要 `positive_label`）</禁止>
- <禁止>把事件表写入 `sampling_sources`（`label_event` / `activity_event` / `conversion_event` 写 `null`）</禁止>
- <禁止>跳过语义检索 §1①+②</禁止>
- <禁止>从 §3.1 positive_label 画像段取值</禁止>

填写后直接去 §4 示例和 §5 完成检查。

---

## 4. 示例

```json
{
  "database": "<来自任务>",
  "T0": "<来自任务>",
  "game_scope": { "target": "<目标游戏>", "similar_games": [] },
  "y_label": { "family": "付费/收入", "task_type": "binary_classification", "event_table": "<语义确认>" },
  "sampling_sources": { "user_table": "...", "label_event": "...", "activity_event": "...", ... },
  "keys": { "user_key_default": "<语义确认>", "user_key_behavior": "<语义确认>", "game_key_default": "<语义确认>", "event_time": "<语义确认>", "similar_dim": "<语义确认>" },
  "sql_fragments": {
    "user_key_expr": "assumeNotNull(<user_key_default>)",
    "valid_user": "<user_key_default> IS NOT NULL AND <user_key_default> != ''",
    "game_key_expr": "assumeNotNull(<game_key_default>)",
    "game_filter": "<game_key_default> = '<game_scope.target>'",
    "label_window": "parseDateTimeBestEffortOrNull(<event_time>) > parseDateTimeBestEffortOrNull('<T0>') AND parseDateTimeBestEffortOrNull(<event_time>) <= parseDateTimeBestEffortOrNull('<T0+N>')",
    "positive_label": "<枚举列> = '<画像取值>'",
    "pre_t0_lookback": "parseDateTimeBestEffortOrNull(<event_time>) > parseDateTimeBestEffortOrNull('<T0-L>') AND parseDateTimeBestEffortOrNull(<event_time>) <= parseDateTimeBestEffortOrNull('<T0>')",
    "through_t0": "parseDateTimeBestEffortOrNull(<event_time>) <= parseDateTimeBestEffortOrNull('<T0>')"
  },
  "negative_populations": [ { "code": "N1", "neg_k": 0 }, ... ],
  "source_table_inventory": { "tables": ["<源表1>", "<源表2>", "..."] },
  "inventory_check": { "ok": true, "table_count": "<len(source_table_inventory.tables)>" },
  "projections": [
    { "table": "<源表>", "type": "user_table", "user_key": "..." },
    { "table": "<源表>", "type": "user_keyed", "user_key": "..." },
    { "table": "<源表>", "type": "game_keyed" }
  ]
}
```

---

## 5. 完成检查

- [ ] `step1_0_table_schema.json` 已写；`tables[].name` 与 `system.tables`（源表排除 `step1_%`）一致
- [ ] `step1_0_sampling_plan.json` 已写；`source_table_inventory` / `projections` 1:1 且来自 schema
- [ ] `inventory_check.ok == true` 且 `table_count == len(source_table_inventory.tables) == len(projections)`
- [ ] `y_label`、`sampling_sources`、`keys`、`sql_fragments` 已填；每张投影 `type` 仅为 user_table / user_keyed / game_keyed
