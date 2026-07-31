# step1_6: finalize（定稿）

<入口规则>两种模式均执行本步</入口规则>

**目的**：验收前面所有步骤的产出无误后，将 schema 定稿为 **`step1_output_meta.json`**，再写 **`receipt.json`** 交给 workflow。本步不写 ClickHouse SQL、不生成 `.py`、不发起 Bash 连库。

## 前置

确认以下文件/表就绪，缺一回对应步骤修复。定稿前 `read_file` `step1_sample_stats.json` 和 plan，用文件值填 receipt，禁止凭记忆。

| 前置条件 | 检查要点 |
|---|---|
| `step1_0_sampling_plan.json` | `inventory_check.ok == true`；cold_start 时 `similar_games` 或 fallback 已写入 |
| `step1_0_table_schema.json` | 已落盘；`tables[]` 每表 `columns` 非空；含 `join_hints` / `role_candidates` / `column_aliases` |
| `step1_sample_stats.json` | `table_count_check.ok == true`，`missing_tables` 为空 |
| 全部交付表 | output_database 实库中与源表同名的表数 = `inventory_check.table_count`；gate 通过 |

**`table_count_check.ok != true` 时禁止写 `step1_output_meta.json` / receipt，也不写入任何其他文件，直接结束执行。编排器检测不到 receipt 会自动将本步标记为 failed 并重试。**

---

## 写出 step1_output_meta.json（schema 定稿）

验收通过后，`read` `step1_0_table_schema.json`，将其 **完整内容原样** `write` 为 **`step1_output_meta.json`**（当前 job workspace）。

<必须>
- `step1_output_meta.json` 与 `step1_0_table_schema.json` **结构与字段值完全一致**（同构副本，供下游语义交接）
- **禁止**往 `step1_output_meta.json` 里塞 `label_stats` / `projection_tables` / `table_count_check` 等统计字段（那些只在 `step1_sample_stats.json`）
- **禁止**改写、精简或重排 schema 字段；以 step1_0 落盘内容为准（若 step1_4 曾回写 schema，以磁盘上最新版为准）
</必须>

结构与 `step1_0_table_schema.json` 相同，例如：

```json
{
  "source_database": "<source_database>",
  "table_names": ["<表名1>", "…"],
  "tables": [
    {
      "name": "<表名>",
      "description": "<表用途描述>",
      "columns": [
        { "name": "<列名>", "valueType": "<类型>", "description": "<含义>", "isPrimaryKey": false }
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

---

## 写 receipt.json

`step1_output_meta.json` 写好后，再写入当前 job workspace。`receipt.json` 仅顶层 `summary` + `artifacts`。

`artifacts` 条目两种形态（勿混用）：

| kind | 字段 | 本步至少登记 |
|---|---|---|
| `file` | `path`（job workspace 内真实相对路径）、`type` | `step1_output_meta.json`（`type: "meta"`）+ `step1_sample_stats.json`（`type: "stats"`） |
| `clickhouse_table` | `uri` = `clickhouse://<output_database>/<table>`，`name` = `<output_database>.<table>` | 全部交付表（与源表同名） |

ClickHouse 表不要写成 `path`，也不要写到只读共享产物区（完成后由平台发布）。

### 示例

```json
{
  "summary": "采样完成：<table_count> 张交付表，<total_users> 用户，<mode>，库 <output_database>",
  "artifacts": [
    {"kind": "file", "path": "step1_output_meta.json", "type": "meta"},
    {"kind": "file", "path": "step1_sample_stats.json", "type": "stats"},
    {"kind": "clickhouse_table", "uri": "clickhouse://<output_database>/<表1>", "name": "<output_database>.<表1>"},
    {"kind": "clickhouse_table", "uri": "clickhouse://<output_database>/<表2>", "name": "<output_database>.<表2>"}
  ]
}
```

`summary` 中的 `<table_count>` / `<total_users>` / `<mode>` / `<output_database>` 取自 `step1_sample_stats.json`。

---

## 完成检查

- step1_1…step1_5 按序完成（prelabeled 跳过 step1_1/step1_2）；step1_5 自查无未修异常
- cold_start：step1_2 已完成且 `similar_games` 或 fallback 已记入 plan
- `inventory_check.ok == true`；output_database 实库交付表数 = `inventory_check.table_count`
- `step1_sample_stats.table_count_check.ok == true`
- `step1_output_meta.json` 已写，且与 `step1_0_table_schema.json` 同构一致
- `mode != "prelabeled"`（主路径）：用户表列 = 源列 + `label`（追加）。`mode == "prelabeled"`（prelabeled 分支）：用户表保留源 `label_column` 列、未重复追加
- receipt 含 `step1_output_meta.json` + `step1_sample_stats.json`（file）+ 全部交付表（clickhouse_table）；无额外顶层字段

失败：回对应步骤修复后重跑。不写 receipt，不落盘任何文件。编排器会自动感知失败并重试。
