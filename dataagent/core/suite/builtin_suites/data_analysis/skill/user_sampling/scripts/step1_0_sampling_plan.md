# step1_0: 采样计划

**目的**：先落盘 `step1_0_table_schema.json`，再落盘 `step1_0_sampling_plan.json`。

**全表投影**：<必须>`projections[]` 与源表一一对应，一张都不能少</必须>；step1_4 为每张源表在 output_database 建同名表，全表投影。

**核心原则**：所有语义查询结果**立刻落盘到独立文件**，写 schema 时只从文件读、对着抄，禁止凭对话记忆写任何一个字段。

---

## 本步做什么

1. **ClickHouse 取全局表名列表**（最先做，唯一权威清单）→ 写入 `source_table_inventory.tables`。
2. **语义批查**：表名按 ≤8 张分组，每批一次 `semantic_retrieve`，同时问列结构 + JOIN 关系 + 业务角色。返回的完整 JSON **立刻 `Write` 到 `step1_semantic_batch_NN.json`**（`01`、`02`… 两位编号）。
3. **覆盖检查**：所有批查完成后，核对 CH 清单是否全覆盖；列结构有缺口则用 `system.columns` 批查补齐。
4. **写 schema**：先 `ls` 确认所有 `step1_semantic_batch_NN.json` 已落盘，再逐个 `Read`，从中**机械抄写** `description`、`join_hints`、`role_candidates` 到 `step1_0_table_schema.json`。
5. **判定 mode**：查用户表 label 列情况 → `prelabeled` 或 `regular`。据此写出 `step1_0_sampling_plan.json`。
6. <重要>`mode=="prelabeled"` → 跳至 step1_3；否则 → step1_1</重要>。

<必须>本步所有 `semantic_retrieve` **必须串行**：同一时刻只允许 1 个在途请求；等返回后再发下一批，禁止并行。</必须>

---

## plan 字段

> `source_database`（源库，只读）/ `output_database`（产物库）由上游传入；`mode` 为 `"regular"` / `"cold_start"` / `"prelabeled"`；`run_id`/`T0`/`label_window_days`/`lookback_days`/`sample_size` 来自任务参数，`cold_start_threshold` 默认 500；其余字段规则见对应章节。

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

---

## 1. 数据 schema 落盘

### 0. 全局表名列表（最先做）

```sql
SELECT name FROM system.tables
WHERE database = '{{source_database}}'
ORDER BY name
```

写入 `source_table_inventory.tables`。`inventory_check.table_count` = 该列表长度。
<必须>CH 表名清单是后续语义查询的**唯一表名来源**；禁止在未获取此清单前发起 `semantic_retrieve`，也禁止发自由关键词式语义查询，必须以 CH 清单中的具体表名逐张注入 query。</必须>

---

### 1. 语义批查 + 立即落盘

<必须>语义服务单批不稳定，必须分批查询，每批 ≤8 张且不重复</必须>。

每批 `semantic_retrieve` 一次问清三件事——列结构、JOIN 关系、业务角色。query 固定为：

```text
查询 <source_database> 数据库以下表的语义信息：<t1>, <t2>, …
目标游戏：<target_game>
```

流程：

1. 从 `source_table_inventory.tables` 取出全部表名，每批 ≤8 张分组
2. 按组**串行**发 `semantic_retrieve`：发完一批，等返回
3. 返回后**立刻落盘**——把语义返回的完整 JSON 用 `Write` 写入 `step1_semantic_batch_NN.json`（`NN` 从 `01` 开始两位编号，如 `step1_semantic_batch_01.json`）
4. 继续下一批，重复 2–3
5. 全部分批完成后，对照 CH 清单检查覆盖：重算 `missing_names`（CH 清单中有但任何 dump 文件里都没出现的表）和 `missing_columns`（表名出现了但列结构不全）；若任一项非空 → 立即走 CH 批查补齐（不再追加语义查询）

#### 列缺口 CH 批查（唯一兜底）

对 `missing_columns`（如有 `missing_names` 一并包含）**一次**提交：

```sql
SELECT table, name, type
FROM system.columns
WHERE database = '{{source_database}}'
  AND table IN (/* 缺口表名 */)
ORDER BY table, position
```

将结果写回对应 `tables[].columns`（`name`←`name`，`valueType`←`type`；`description`/`isPrimaryKey` 可空）。

<禁止>使用 `is_in_primary_key`、`ordinal_position` 等可能不存在的元数据列</禁止>。
语义侧的表用途描述可保留；**列清单以 CH 为准**。

---

### 覆盖定义（硬门禁）

CH 清单里有这张表名，不代表我们已经拿到了它的列结构。<必须>下面两条同时满足，才能说覆盖完成，才能进入写 schema</必须>：

1. **表名未遗漏**：`missing_names` 为空。CH 清单中的每一张表，在语义查询结果中都出现了，一张不少。
2. **每表的列结构到位**：`missing_columns` 为空。对 CH 清单中的任意一张表，以下三点同时满足才算到位：
   - `columns` 数组非空（`columns.length >= 1`）
   - 每条列都有 `name` 字段
   - 若 `valueType` 缺失，已用 CH `system.columns` 兜底补齐

<禁止>
- 把 `answerGuidance`、diagnostic、toolTrace、或 `behavior_1~7` 这类无列名无类型的字段当成列结构来凑数
- 表名数量对上了就写「全覆盖」，无视 `missing_columns` 不为空
- `missing_columns` 还没清空就判定 mode 或写 plan
- 在拿到 CH 表名清单之前发送任何 `semantic_retrieve`
- 发自由关键词式语义查询；query 中必须注入 CH 实表名
</禁止>

---

### 写 schema

覆盖门禁通过后，写出 **`step1_0_table_schema.json`**。

<必须>写 schema 前三步走，漏一步视为未完成</必须>：
1. `ls` 确认所有 `step1_semantic_batch_*.json` 文件已落盘，数量与批次数一致
2. **逐个 `Read`** 每一个 dump 文件（不跳、不凭记忆），读完再汇总
3. 最后 `Write` `step1_0_table_schema.json`

<下面的约束非常重要!!!/>
<重要>写 schema 时**禁止凭记忆**。所有内容必须来自第 2 步 `Read` 到的 dump 文件内容——对着原文搬运，不缩写、不改写、不补全。</重要>

#### description 保真

<必须>以下规则逐字遵守；违反任意一条即视为落盘失败，须 Read 对应 dump 文件重写该列 description。</必须>

| 规则 | 说明 |
|------|------|
| **逐字搬运** | `columns[].description` <必须>与 dump 文件中语义返回的 `dataAccessPlan.tables[].columns[].description` **逐字符一致**</必须>（含空格、标点、括号、枚举全文），不得改写、摘要、截断 |
| **括号/枚举全保留** | 含括号内枚举值、编码映射、补充说明时，**整段原样写入**，禁止只留括号前短标题 |
| **排序/TopN说明保留** | 含 `TOPN`、`按XX排序`、`截断` 等说明时，**全量保留** |
| **语义无描述则留 null** | 语义侧为 `null` 时写 `null`，**不得**用列名或自拟短词填上 |

<禁止>
- **禁止**去掉括号内的枚举/映射/补充说明
- **禁止**缩写为短标签（完整描述截成前缀）
- **禁止**概括语义已列出的值列表
- **禁止**在语义有描述时自行改写、换同义词、重新措辞
- **禁止**把 description 写成列名本身
</禁止>

<错误示例>
<错误> 语义返回包含括号内完整枚举，写入时只取了括号前的短标题
<错误> 语义返回列出了一系列枚举值，写入时替换成了概括性的简短标签
<错误> 语义返回了完整的时间和排序说明，写入时只保留了列名本身
</错误示例>

#### join_hints 保真

<必须>写 `join_hints` 时，逐个 Read 所有 `step1_semantic_batch_NN.json`，从各文件的 `dataAccessPlan.joinPaths` 中收集，按 `left` + `right` + `on` 去重后写入。</必须>

映射规则：
- 每条 joinPath → 一条 hint：`left` / `right` = `<表名>.<列名>`（去掉库前缀，列名从 `on` 解析）
- `note` 可写简短说明
- left/right 顺序与语义返回一致，禁止调转
- 所有 dump 文件中都找不到 joinPaths → `join_hints` 写 `[]`
- 禁止因多表都有同名键列而自行加边——文件里没写的表对，不补

#### role_candidates

<必须>从所有 `step1_semantic_batch_NN.json` 中，收集语义返回的每张表业务角色，写入 `role_candidates`</必须>（五键齐全）：

| 角色 | 要求 |
|---|---|
| `user_table` | <必须>非空 |
| `game_dim` | <必须>非空 |
| `label_event` / `activity_event` / `conversion_event` | regular 尽量填；prelabeled 可为 `[]` |

<禁止>要求全局每张表都分到上述五类</禁止>。其余表只进入 `tables[]`，后续在 `projections[].type` 标 `user_keyed` / `game_keyed`。角色无法从语义确认时，用已落盘的列名约定推断并写入，不阻塞。

</上面的约束非常重要!!!>

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
    "user_id_columns": ["<如 usid>", "<rank_flg>", "<dsid>"]
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

`mode` 写 `"prelabeled"`；`y_label.event_table` / `sampling_sources.label_event` / `sampling_sources.activity_event` / `sampling_sources.conversion_event` / `sql_fragments`（除 `user_key_expr`、`valid_user`、`game_filter` 外）写 `null`；`negative_populations` 写 `[]`。具体：

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

`sampling_sources` / `keys` / `sql_fragments` 依据 `role_candidates` / `tables`（`join_hints` 仅作下游参考，采样阶段不依赖它构造 SQL）。

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

- [ ] 文件已写出：`step1_0_table_schema.json`、`step1_0_sampling_plan.json`
- [ ] 语义 dump 文件齐全：`step1_semantic_batch_01.json`、`02`… 与批次数对应
- [ ] schema 覆盖：`table_names` 与 CH 清单一一对应，每张表 `columns.length >= 1`
- [ ] description 保真：每列的 description 来自 dump 文件逐字搬运，没返回的就写 `null`，不缩写不重写
- [ ] join_hints 保真：每条 hint 都能在 dump 文件的 `joinPaths` 中找到对应，没有凭空多出的表对，全部文件无 joinPaths 时写 `[]`
- [ ] role_candidates 五键齐全：`user_table`、`game_dim`、`label_event`、`activity_event`、`conversion_event`
- [ ] column_aliases：`user_id_columns` 非空
- [ ] plan 完整：`source_table_inventory` 与 `projections` 一一对应
- [ ] inventory 核对：`inventory_check.ok == true`，`table_count == len(projections)`
- [ ] projections 类型合法：每项 `type` 只允许 `user_table` / `user_keyed` / `game_keyed`
