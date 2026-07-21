# step1_6: finalize（定稿）

**目的**：验收前面所有步骤的产出无误后，写 **`receipt.json`** 交给 workflow。本步不写 ClickHouse SQL、不生成 `.py`、不发起 Bash 连库。

## 前置

确认以下文件/表就绪，缺一回对应步骤修复：

| 前置条件 | 检查要点 |
|---|---|
| `step1_0_sampling_plan.json` | `inventory_check.ok == true`；cold_start 时 `similar_games` 或 fallback 已写入 |
| `step1_output_meta.json` | `table_count_check.ok == true`，`missing_tables` 为空 |
| 全部 `step1_sampled_*` 表 | 实库中张数 = `inventory_check.table_count`；gate 通过 |

**`table_count_check.ok != true` 时禁止写 receipt，也不写入任何其他文件，直接结束执行。编排器检测不到 receipt 会自动将本步标记为 failed 并重试。**

---

## 写 receipt.json

验收通过后写入当前 job workspace。`receipt.json` 仅顶层 `summary` + `artifacts`。

`artifacts` 条目两种形态（勿混用）：

| kind | 字段 | 本步至少登记 |
|---|---|---|
| `file` | `path`（job workspace 内真实相对路径）、`type` | `step1_output_meta.json` |
| `clickhouse_table` | `uri` = `clickhouse://<database>/<table>`，`name` = `<database>.<table>` | 全部 `step1_sampled_*` 交付表 |

ClickHouse 表不要写成 `path`，也不要写到只读共享产物区（完成后由平台发布）。

### 示例

```json
{
  "summary": "采样完成：<table_count> 张交付表，<total_users> 用户，<mode>，库 <database>",
  "artifacts": [
    {"kind": "file", "path": "step1_output_meta.json", "type": "meta"},
    {"kind": "clickhouse_table", "uri": "clickhouse://<database>/step1_sampled_<表1>", "name": "<database>.step1_sampled_<表1>"},
    {"kind": "clickhouse_table", "uri": "clickhouse://<database>/step1_sampled_<表2>", "name": "<database>.step1_sampled_<表2>"}
  ]
}
```

---

## 完成检查

- step1_1…step1_5 按序完成（prelabeled 跳过 step1_1/step1_2）；step1_5 自查无未修异常
- cold_start：step1_2 已完成且 `similar_games` 或 fallback 已记入 plan
- `inventory_check.ok == true`；实库 `step1_sampled_*` 张数 = `inventory_check.table_count`
- `step1_output_meta.table_count_check.ok == true`
- `mode != "prelabeled"`（主路径）：用户表列 = 源列 + `label`（追加）。`mode == "prelabeled"`（prelabeled 分支）：用户表保留源 `label_column` 列、未重复追加
- receipt 含 `step1_output_meta.json`（file）+ 全部 `step1_sampled_*`（clickhouse_table）；无额外顶层字段

失败：回对应步骤修复后重跑。不写 receipt，不落盘任何文件。编排器会自动感知失败并重试。
