# step1_0: 采样计划

**目的**：先落盘 `step1_0_table_schema.json`，再落盘 `step1_0_sampling_plan.json`。

**全表投影**：<必须>`projections[]` 与源表一一对应，一张都不能少</必须>；step1_4 为每张源表在 output_database 建同名表，全表投影。

**核心原则**：整库本体可用一次 `semantic_download` 落盘，再由 skill 自带脚本生成 schema（格式与 `step1_output_meta.json` 同构）。按 query 检索仍可用 MCP `semantic_retrieve`。

---

## 本步做什么

1. **ClickHouse 取全局表名列表**（最先做，唯一权威清单）→ `write` `step1_ch_tables.json`，并记入 `source_table_inventory.tables`。
2. **取语义**：整库本体用 `semantic_download`（`scene_name` 取任务 JSON / 用户 query 的 `scene_name` 或 `scene`），正文写入 `.dataagent/tool_outputs/semantic_download_<timestamp>.txt`。按 query 检索用 MCP `semantic_retrieve`。
3. **脚本写 schema**：运行 `scripts/build_table_schema.py`，产出 `step1_0_table_schema.json`。
4. **列缺口兜底**：脚本退出码 2 时，对缺口表一次 `system.columns` 批查 → `write` `step1_ch_columns.json` → 带 `--columns-file` 再跑脚本。
5. **脚本写 plan 骨架**：运行 `scripts/build_sampling_plan.py`，产出 `step1_0_sampling_plan.json`（`mode` 仍为 null）和 `step1_0_sql/mode_probe.sql`。`projections[]`、`user_key`、`sql_fragments.user_key_expr` / `valid_user` / `game_filter` 由脚本从 schema + 任务参数生成，不要手写 21 表计划。
6. **判定 mode**：提交 probe SQL（用户表无 label 列则跳过 probe，直接 `--mode regular`）。`pos_users > 0` 且 `neg_users > 0` → `--mode prelabeled`，否则 `--mode regular`。
7. <重要>`mode=="prelabeled"` → 跳至 step1_3；`regular` 先补 `y_label.family` / `positive_label` / `negative_populations`，再进入 step1_1</重要>。

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
    "game_key_behavior": "<string>",
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

将 `collect_job` 结果 `write` 为工作区 **`step1_ch_tables.json`**（JSON 数组，或 `{"tables":[...]}` / `[{"name":"..."}]`）。`source_table_inventory.tables` 与该清单一致。`inventory_check.table_count` = 该列表长度。

<必须>CH 表名清单是表集合的唯一来源；先落盘清单，再 `semantic_download`。</必须>

---

### 1. 一次 download + 落盘

`scene_name`：任务 JSON / 用户 query 里的 `scene_name` 或 `scene`。`source_database` 是 ClickHouse 库名，给脚本 `--source-database`。

调用一次（本地工具）：

```text
semantic_download(scene_name=<scene_name>)
```

工具把 HTTP 正文写入当前任务 workspace 的 `.dataagent/tool_outputs/semantic_download_<timestamp>.txt`。给模型的只是 `{ok, path, bytes, top_level_keys}`。`--dump` 用回执里的 `path`。`write_file` 的 `content` 必须由模型填进参数，接不住上一个工具的返回值，所以 **不要** 用 `write_file` 抄 dump。确认回执 `ok == true` 且该路径文件已存在后，进入脚本转 schema。

---

### 2. 脚本生成 schema

`ls` 确认回执 `path` 与 `step1_ch_tables.json` 已落盘后，用 Bash 跑 skill 自带脚本（不要自写转换脚本）：

```bash
python skill/user_sampling/scripts/build_table_schema.py \
  --dump <semantic_download 回执 path> \
  --source-database {{source_database}} \
  --inventory-file step1_ch_tables.json \
  --out step1_0_table_schema.json
```

脚本会把本体里的表/列/描述/JOIN 写成与 `step1_output_meta.json` 同构的 schema：`table_names` 对齐 CH 清单，`columns[].description` 从 dump 原样带出（语义无描述则为 `null`），`join_hints` 从 relations / joinPaths 解析，`role_candidates` / `column_aliases` 按 `skill/user_sampling/config.yaml` 匹配清单（脚本默认读该文件，不必传 `--config`）。

#### 列缺口 CH 批查（脚本退出码 2）

stderr 会打印 `missing_columns=[...]`。对这些表**一次**提交：

```sql
SELECT table, name, type
FROM system.columns
WHERE database = '{{source_database}}'
  AND table IN (/* 缺口表名 */)
ORDER BY table, position
```

`write` 为 `step1_ch_columns.json`（`[{"table","name","type"}, ...]`），再跑：

```bash
python skill/user_sampling/scripts/build_table_schema.py \
  --dump <semantic_download 回执 path> \
  --source-database {{source_database}} \
  --inventory-file step1_ch_tables.json \
  --columns-file step1_ch_columns.json \
  --out step1_0_table_schema.json
```

<禁止>使用 `is_in_primary_key`、`ordinal_position` 等可能不存在的元数据列</禁止>。
CH 补上的列 `description` / `isPrimaryKey` 可空；**列清单以 CH 为准**，本体已有的 description 由脚本保留。

---

### 覆盖定义（硬门禁）

脚本成功（退出码 0）且磁盘上 `step1_0_table_schema.json` 满足下面两条，才能判定 mode / 写 plan：

1. **表名未遗漏**：`table_names` 与 `step1_ch_tables.json` 1:1。
2. **每表的列结构到位**：每张表 `columns.length >= 1`，每条列有 `name`；缺 `valueType` 的已用 `--columns-file` 补齐。

`role_candidates`：`user_table` / `game_dim` 须非空；`label_event` / `activity_event` / `conversion_event` 在 regular 尽量有值，prelabeled 可为 `[]`。其余表只进入 `tables[]`。无用户键的表由脚本标 `game_keyed`：一行一游戏的本体维表套目标游戏过滤；`config.yaml` `projection.catalog_copy` 中的目录维表整表拷贝。

写 plan 时 **`read` 脚本产出的 schema 与 plan**，不要凭记忆改 `projections` / `description` / `join_hints` / `columns`。

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
    "user_id_columns": ["<config.yaml columns.user_id 中实际出现的列>"],
    "game_columns": ["<config.yaml columns.game 中实际出现的列>"],
    "label_columns": ["<config.yaml columns.label 中实际出现的列>"],
    "event_time_columns": ["<config.yaml columns.event_time 中实际出现的列>"]
  }
}
```

<必须>`table_names` 与 `source_table_inventory.tables` 1:1，且每张表 `columns.length >= 1`</必须>。  
<必须>存在 `join_hints`（可为 `[]`）与 `role_candidates`（五键齐全）与 `column_aliases`（`user_id_columns` 非空）</必须>。

---

## 2. 脚本写 plan 骨架

schema 覆盖门禁通过后，用任务参数跑 skill 自带脚本（不要手写 `projections[]` 或 `sql_fragments.user_key_expr` / `valid_user` / `game_filter`）：

```bash
python skill/user_sampling/scripts/build_sampling_plan.py \
  --schema step1_0_table_schema.json \
  --source-database {{source_database}} \
  --output-database {{output_database}} \
  --target-game <target_game> \
  --run-id <run_id> \
  --t0 <T0> \
  --label-window-days <label_window_days> \
  --lookback-days <lookback_days> \
  --sample-size <sample_size> \
  --out step1_0_sampling_plan.json
```

退出码 0 后：

- `step1_0_sampling_plan.json`：`mode` 为 `null`；`projections[]` 与 schema 表名 1:1；用户键 / `valid_user` / `game_filter` 已填
- `step1_0_plan_notes.json`：脚本推断结果 + 模型还需补的字段
- `step1_0_sql/mode_probe.sql`：仅当用户表有 label 列时写出

脚本**不会**填写：`mode`、`y_label.family`、`sql_fragments.positive_label`、`negative_populations`、`keys.similar_dim`。这些要么由 probe 决定，要么只在 regular 由模型按实表取值填写。

## 3. 判定 mode

用户表有 label 列时，提交 `step1_0_sql/mode_probe.sql`（`resource_id="clickhouse"`，顶层 `SELECT`，不要加 `LIMIT 1`）：

```sql
SELECT
  uniqExactIf(<user_key_expr>, <label_column> = <label_pos_val>) AS pos_users,
  uniqExactIf(<user_key_expr>, <label_column> = <label_neg_val>) AS neg_users
FROM {{source_database}}.<user_table>
WHERE <valid_user>
```

占位符已写进生成的 SQL，不要改写。`pos_users > 0` 且 `neg_users > 0` → `prelabeled`；否则 → `regular`。用户表没有 label 列 → 直接 `regular`。

```bash
python skill/user_sampling/scripts/build_sampling_plan.py \
  --schema step1_0_table_schema.json \
  --out step1_0_sampling_plan.json \
  --mode prelabeled
```

或 `--mode regular`。`cold_start` 仍由 step1_1 的正样本计数决定，不要在本步写成 `cold_start`。

### 3.A `--mode prelabeled`

脚本会把 `y_label.event_table` / 事件类 `sampling_sources` / `positive_label` 与时间片段置 `null`，`negative_populations` 置 `[]`。`keys.label_column` 必须已在骨架里。不要对事件表做枚举查询。完成后进入 step1_3。

### 3.B `--mode regular`

脚本填入 `role_candidates` 里的事件表候选，并在能识别 `event_time` 且 `T0` / 窗口参数齐全时写入时间片段。模型还须：

1. 按任务目标写入 `y_label.family`（家族表见下）
2. 对 `y_label.event_table` 查真实枚举后写 `sql_fragments.positive_label`（取值必须出现在查询结果里）
3. 按家族写 `negative_populations`

| 家族 | 关键词 |
|---|---|
| 安装/下载 | 下载、安装、拉新 |
| 付费/收入 | 付费、ARPU |
| 预约 | 预约 |
| 点击/CTR | 点击、CTR |
| 留存/活跃 | 留存、DAU |
| 时长/参与 | 时长、参与 |

负样本默认：付费 **N4**；CTR **N2**；安装/留存/时长 **N3+N2**；至少一 hard + **N5**。

`sampling_sources` / `keys` / `sql_fragments` 以脚本产出为准；只改 notes 里列出的待填项。

### 3.1 sql_fragments 构造规则

骨架已生成的片段不要手改。下表仅用于 regular 补时间片段（脚本没写出时）和 `positive_label`。

每个片段必须是可直接拼入 ClickHouse WHERE / SELECT 的表达式。列名与类型取自 `step1_0_table_schema.json`。

| 片段 | 规则 | 示例 |
|---|---|---|
| `user_key_expr` | 脚本已生成：String 用 `assumeNotNull(<col>)`，数值用 `<col>` | `assumeNotNull(user_id)` |
| `valid_user` | 脚本已生成：滤 NULL（String 加 `!= ''`） | `user_id IS NOT NULL AND user_id != ''` |
| `game_key_expr` | 同用户键规则 | `assumeNotNull(game_id)` |
| `game_filter` | 行为/转化事件的游戏范围过滤；列取 `keys.game_key_behavior`。脚本初始生成目标游戏单点条件，cold_start 在 step1_2 扩成目标 + 相似游戏 | `game_name = 'genshin'` |

#### 时间片段（仅 regular，脚本未写出时按此补）

| 片段 | 窗口 | 写法（`<tc>`=`keys.event_time`；String 包 `parseDateTimeBestEffortOrNull(<tc>)`） |
|---|---|---|
| `label_window` | `(T0, T0+N]` | `<tc> > <T0> AND <tc> <= <T0 + N>` |
| `pre_t0_lookback` | `(T0 - L, T0]` | `<tc> > <T0 - L> AND <tc> <= <T0>` |
| `through_t0` | `≤ T0` | `<tc> <= <T0>` |

`<T0>`/`<N>`/`<L>` = `T0` / `label_window_days` / `lookback_days`。

#### positive_label（仅 regular）

标定正样本的 WHERE（不含时间窗）。写完 family 后对 `y_label.event_table`：

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

- [ ] 文件已写出：`step1_ch_tables.json`、`semantic_download` 回执 `path`（`.dataagent/tool_outputs/semantic_download_*.txt`）、`step1_0_table_schema.json`、`step1_0_sampling_plan.json`
- [ ] schema 由 `scripts/build_table_schema.py` 生成（退出码 0），不是手写
- [ ] schema 覆盖：`table_names` 与 CH 清单一一对应，每张表 `columns.length >= 1`
- [ ] role_candidates 五键齐全：`user_table`、`game_dim`、`label_event`、`activity_event`、`conversion_event`
- [ ] column_aliases：`user_id_columns` 非空
- [ ] plan 由 `scripts/build_sampling_plan.py` 生成后 `--mode prelabeled|regular` 定稿，不是手写 `projections[]`
- [ ] `mode` 已是 `prelabeled` 或 `regular`（`cold_start` 留给 step1_1）
- [ ] plan 完整：`source_table_inventory` 与 `projections` 一一对应
- [ ] inventory 核对：`inventory_check.ok == true`，`table_count == len(projections)`
- [ ] projections 类型合法：每项 `type` 只允许 `user_table` / `user_keyed` / `game_keyed`
