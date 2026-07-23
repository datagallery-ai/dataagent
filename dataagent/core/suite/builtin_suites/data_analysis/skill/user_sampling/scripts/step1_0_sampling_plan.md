# step1_0: 采样计划

**目的**：先落盘 `step1_0_table_schema.json`，再落盘 `step1_0_sampling_plan.json`。

**全表投影**：<必须>`projections[]` 与源表一一对应，一张都不能少</必须>；step1_4 为每张源表在 output_database 建同名表，全表投影。

---

## 本步做什么

1. **ClickHouse 取全局表名列表**（最先做，唯一权威清单）→ `source_table_inventory.tables`。
2. **① 全库表+列结构**（可附带 join；禁止探索性预查）→ 列覆盖门禁通过（§覆盖定义）→ 必要时 `system.columns` 批查。
3. **② 角色定位** → 写 `role_candidates` → 落盘完整 schema。
4. 判定 mode，写出 `step1_0_sampling_plan.json`。
5. <重要>`mode=="prelabeled"` → step1_3；否则 → step1_1</重要>。

---

## plan 字段

> `run_id`/`T0`/`label_window_days`/`lookback_days`/`sample_size` 来自任务参数；`cold_start_threshold` 默认 500。  
> `source_database` 存放原始业务表（只读），`output_database` 存放 step1 产物表。  
> `mode`：`"regular"` / `"cold_start"` / `"prelabeled"`。  
> `game_scope`：`target` 目标游戏名，`similar_games` 由 step1_2 填写。  
> `y_label`：`family` 对应 `labels.md` 家族名，`task_type` 固定 `"binary_classification"`。  
> `sampling_sources`：角色→表名，`game_dim` 是游戏维度表。  
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

## 1. 数据 schema 落盘

### 0. 全局表名列表（最先做）

```sql
SELECT name FROM system.tables
WHERE database = '{{source_database}}'
ORDER BY name
```

写入 `source_table_inventory.tables`。`inventory_check.table_count` = 该列表长度。
<必须>CH 表名清单是后续语义查询的**唯一表名来源**；禁止在未获取此清单前发起 `semantic_retrieve`，也禁止发自由关键词式语义查询（如 "数据库 <source_database> 有哪些表"），必须以 CH 清单中的具体表名逐张注入 query。</必须>

---

### 覆盖定义（硬门禁）

表名出现 ≠ 结构齐备。<必须>仅当下列全部成立，才可宣称「① 列结构覆盖完成」并进入 ②</必须>；写 plan 前还须完成 ② 与 schema 落盘：

1. **`missing_names` 为空**：`CH清单 \ 已合并结果中的表名`
2. **`missing_columns` 为空**：已出现但 `columns` 为空数组、或任一条目缺 `name` 的表
3. 每张表 `columns.length >= 1`；每列至少有 `name`（`valueType` 缺失则必须已用 CH 兜底补齐）

<禁止>
- 用 `answerGuidance`、diagnostic / toolTrace、或「behavior_1~7」等概括语充数
- 仅因表名与 CH 清单对齐就写「25 已齐 / 全覆盖」
- 在 `missing_columns` 非空时进入 ②、判定 mode 或写 plan
- CH 清单之前或 ①② 之外的探索性 `semantic_retrieve`
- 单独再开「全库 JOIN」语义轮
- 发自由关键词式语义查询（如 "数据库 xxx 有哪些表"）；语义 query 中必须注入 CH 实表名
</禁止>

---

### ① 全库表 + 列结构（含可选 join）

<必须>语义服务每次最多返回 10 张表，必须分批查询，每批 ≤10 张且不重复</必须>。

query 固定为（每批注入对应表名）：

```text
数据库 <source_database> 中的下表，逐表列出：
- 表名、表用途描述
- 全部列名、每列数据类型（String/Int64/Float64/Date/DateTime 等）、业务含义
- 主键
若能确认表间关联，可附带 JOIN 线索；无法确认时省略，不要猜测。
表：<t1>, <t2>, …
```

流程：

1. 从 `source_table_inventory.tables` 取完整表名列表，按 ≤10 张拆分为多批
2. 各批**并行**发出 `semantic_retrieve`，合并进工作副本
3. 合并后重算 `missing_names` / `missing_columns`；若仍有 → 立刻走 CH 批查（禁止再开语义轮补查）
4. 两层差集皆空 → 结束 ①

有可靠关联则写入 `join_hints[]`，否则 `[]`（不要求覆盖全部表，不阻塞）。

#### 列缺口 CH 批查（① 唯一兜底）

对 `missing_columns`（如有 `missing_names` 一并包含）**一次**提交：

```sql
SELECT table, name, type
FROM system.columns
WHERE database = '{{source_database}}'
  AND table IN (/* 缺口表名 */)
ORDER BY table, position
```

将结果写回对应 `tables[].columns`（`name`←`name`，`valueType`←`type`；`description`/`isPrimaryKey` 可空）。

<禁止>使用 `is_in_primary_key`、`ordinal_position` 等可能不存在的元数据列</禁止>。单表备选：`DESCRIBE TABLE {{source_database}}.<table>`。  
语义侧的表用途描述可保留；**列清单以 CH 为准**。

---

### ② 角色定位

<必须>仅在 ① 覆盖门禁通过后执行</必须>。

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

写入 `role_candidates`（键必须齐全）：

| 角色 | 要求 |
|---|---|
| `user_table` | <必须>非空 |
| `game_dim` | <必须>非空 |
| `label_event` / `activity_event` / `conversion_event` | regular 尽量填；prelabeled 可为 `[]` |

<禁止>要求全局每张表都分到上述五类</禁止>。其余表只进入 `tables[]`，后续在 `projections[].type` 标 `user_keyed` / `game_keyed`。角色无法从语义确认时，用已落盘的列名约定推断并写入，不阻塞。

---

### 落盘

写出 **`step1_0_table_schema.json`**：

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
  },
  "column_aliases": {
    "user_id_columns": ["usid", "rank_flg", "dsid"]
  }
}
```

<必须>`table_names` 与 `source_table_inventory.tables` 1:1，且每张表 `columns.length >= 1`</必须>。  
<必须>存在 `join_hints`（可为 `[]`）与 `role_candidates`（五键齐全）与 `column_aliases`（`user_id_columns` 非空）</必须>。

---

### 键表达式预构造（模式判定前）

从 schema 提取用户键列名与类型，按 §3.1 构造 `user_key_expr`、`valid_user`（此时不需要 `game_key_expr` / `game_filter`）。

## 2. 判定 mode：查用户表是否已有 label

- **prelabeled**：用户表已有 label 列且 0/1 两侧都有数据 → 跳过 step1_1/step1_2
- **regular**：否则走事件口径（step1_1 正样本 < 500 时降级 `cold_start`）

**前置**：schema 已落盘且覆盖门禁通过；`user_key_expr` / `valid_user` 已预构造。

```sql
SELECT
  uniqExactIf(<user_key_expr>, <label_column> = <label_pos_val>) AS pos_users,
  uniqExactIf(<user_key_expr>, <label_column> = <label_neg_val>) AS neg_users
FROM {{source_database}}.<user_table>
WHERE <valid_user>
```

`<label_pos_val>`/`<label_neg_val>`：Int* → `1`/`0`，String → `'1'`/`'0'`。  
`pos_users > 0` 且 `neg_users > 0` → `mode="prelabeled"`（§3.A）；否则 `mode="regular"`（§3.B）。  
**禁止加 LIMIT 1**。

## 3. 填写 plan

`read` schema + 任务参数 → 按 mode 二选一 → `write`/`edit` `step1_0_sampling_plan.json`。

### 3.A prelabeled（`mode=="prelabeled"`）

按上方 plan JSON 的 <必须>prelabeled 填法</必须> 逐字段填写：

- <必须>`keys.label_column` 必填</必须>（以 schema 实列为准）
- <必须>仅当 `projections[]` 有 `game_keyed` 时才构造 `sql_fragments.game_filter`</必须>
- <禁止>对事件表 MCP `SELECT` 枚举画像</禁止>
- <禁止>把事件表写入 `sampling_sources`</禁止>

### 3.B regular（`mode!="prelabeled"`）

| 家族 | 关键词 |
|---|---|
| 安装/下载 | 下载、安装、拉新 |
| 付费/收入 | 付费、ARPU |
| 预约 | 预约 |
| 点击/CTR | 点击、CTR |
| 留存/活跃 | 留存、DAU |
| 时长/参与 | 时长、参与 |

负样本默认：付费 **N4**；CTR **N2**；安装/留存/时长 **N3+N2**；至少一 hard + **N5**。

填写顺序：顶层参数 → `game_scope` → `y_label` → `sampling_sources` → `keys` → `sql_fragments` → `negative_populations` → `source_table_inventory` → `projections[]` → `inventory_check`。

`sampling_sources` / `keys` / `sql_fragments` 依据 `role_candidates` / `join_hints` / `tables`。

### 3.1 sql_fragments 构造规则

每个片段必须是可直接拼入 ClickHouse WHERE / SELECT 的表达式。列名与类型取自 `step1_0_table_schema.json`。

| 片段 | 规则 | 示例 |
|---|---|---|
| `user_key_expr` | String：`assumeNotNull(<col>)`；数值：`<col>` | `assumeNotNull(user_id)` |
| `valid_user` | 滤 NULL（String 加 `!= ''`） | `user_id IS NOT NULL AND user_id != ''` |
| `game_key_expr` | 同用户键规则 | `assumeNotNull(game_id)` |
| `game_filter` | String：`= '<target>'`；Int*：`= <target>` | `game_id = 'genshin'` |

#### 时间片段（仅 regular）

| 片段 | 窗口 | 写法（`<tc>`=`keys.event_time`；String 包 `parseDateTimeBestEffortOrNull(<tc>)`） |
|---|---|---|
| `label_window` | `(T0, T0+N]` | `<tc> > <T0> AND <tc> <= <T0 + N>` |
| `pre_t0_lookback` | `(T0 - L, T0]` | `<tc> > <T0 - L> AND <tc> <= <T0>` |
| `through_t0` | `≤ T0` | `<tc> <= <T0>` |

`<T0>`/`<N>`/`<L>` = `T0` / `label_window_days` / `lookback_days`。

#### positive_label（仅 regular）

标定正样本的 WHERE（不含时间窗）。写 plan 前对 `y_label.event_table`：

```sql
SELECT <enum_col>, count() AS cnt
FROM {{source_database}}.<y_label.event_table>
WHERE <valid_user> AND <game_filter> AND <label_window>
GROUP BY <enum_col>
ORDER BY cnt DESC
LIMIT 20
```

从结果选取取值写入；**禁止**写库中未出现的取值。常见枚举列见 `labels.md`。

---

## 4. 完成检查

- [ ] `step1_0_table_schema.json` 已写；`table_names` 与 CH 清单 1:1
- [ ] <必须>每张表 `columns.length >= 1`</必须>（表名齐 ≠ 结构齐）
- [ ] <必须>`join_hints` 字段存在（可为 `[]`）；`role_candidates` 五键齐全；`column_aliases.user_id_columns` 非空</必须>
- [ ] `step1_0_sampling_plan.json` 已写；`source_table_inventory` / `projections` 1:1
- [ ] `inventory_check.ok == true` 且 `table_count == len(projections)`
- [ ] `projections[]` 每项 `type` 仅为 `user_table` / `user_keyed` / `game_keyed`
