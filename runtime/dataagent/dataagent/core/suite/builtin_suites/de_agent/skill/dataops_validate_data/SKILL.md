---
name: dataops_validate_data
description: |
  Use when (任一即触发):
    1. 独立数据验证 —— 用户给了一条 INSERT 语句(或工作区里的 insert_*.sql),
       要看这条 SQL 写进目标表后的数据是否合理(空值/单一值/比率越界);
    2. 目标表已有历史产出 —— 想直接对那张真实表做数据体检,而不是再跑一遍试算。
  本 skill **只做数据验证**:不建表、不插入、不用临时表替换,直接查真实目标表。
  能力:空值覆盖率(non-null rate) + 单一值覆盖率(single-value ratio)
    + 异常值占比(outlier ratio,仅比率类字段)。
  实现:工具 `dataops_validate_data`(`de_agent/tools/dataops_validate_data_tool.py`),
    校验器注册表在 `actions/tools/local_tool/dataops_quality_check.py`。
  NOT for: 建表 + 试算 + 交付后自动校验(那是 dataops_quality_check skill 的活);
    字段是否存在/取值范围探查(那是 dataops_probe 的活);
    SQL 语法/规范校验(那是 validate_deliverables 的活)。
---

# 独立数据验证 (Standalone Data Validation)

## 核心心智模型

```
   一条 INSERT 语句
        │
        ├─ 解析出：目标表 db.table  +  SELECT 投影字段
        │     └─ 分区条件（有字面日期就用它，否则用默认分区列 + 一周前）
        │
        ▼
   直接查【真实目标表】（只读！）
        │
        ├─ null_rate          空值覆盖率
        ├─ single_value_ratio 单一值覆盖率
        └─ outlier_ratio      异常值占比（仅比率类字段）
        │
        ▼
   Markdown 表格 → 前端展示
```

**与 `dataops_quality_check` skill 的边界**（别用错）：

| | 本 skill（独立数据验证） | `dataops_quality_check` skill |
|---|---|---|
| 入口 | `dataops_validate_data` | `trial_compute_table` / 交付后 post-hook |
| 建表 | **不建** | 按 `create_*.sql` 建临时表 |
| 插入 | **不插** | 试算 INSERT 到临时表 |
| 查哪张表 | **真实目标表** `db.table` | `adhoctemp.tmp_*` 临时表 |
| 临时表替换 | **不做** | 做 |

## 什么时候用

- 用户直接甩了一条 INSERT 过来：「这条 SQL 写出来的数据看着对吗？」
- 目标表里已经有历史产出，想直接体检，不用再跑一遍试算。
- 交付前自查：想让 agent 主动对某个目标表跑一次数据体检。

**不该用的场景**：要「建表 + 试算 + 校验 + 清理」这一整套 —— 那是
`dataops_quality_check` skill 的 `trial_compute_table`（或交付后的 post-hook）。

## 典型调用

```python
# 推荐：传文件路径，避免大段 SQL 进 chat history
dataops_validate_data(
    sql_file_path="<workspace>/sql/insert_ads_user_profile_ds.sql",
)

# 或者直接贴 SQL
dataops_validate_data(
    sql="INSERT OVERWRITE TABLE biads.ads_user_profile_ds "
        "SELECT udid, app_dl_retention_ratio_1d_udid FROM dws.src WHERE pt_d = '20261020'",
)
```

参数：

| 参数 | 必填 | 说明 |
|---|---|---|
| `sql` | 二选一 | INSERT 语句文本 |
| `sql_file_path` | 二选一 | 工作区里 `insert_*.sql` 的路径（**推荐**） |
| `target_table` | 否 | 目标表 `db.table`；留空则从 INSERT 解析 |
| `partition_filter` | 否 | 分区条件，如 `pt_d = '20261020'`；留空则从 INSERT 解析 |

## 关键约束（Must Follow）

1. **只读**。本工具不建表、不插入、不删表、不改真实数据。它只是查表。
   不要在调用前先「建一张临时表再验证」—— 那是另一条链路。
2. **只认 INSERT**。`CREATE` / `SELECT` / 空语句会**优雅跳过**（`skipped=True`），
   并返回一句人话提示。不要因为「被跳过了」就重试或改写 SQL。
3. **目标表必须是真实表**。工具从 INSERT 的 `INSERT OVERWRITE TABLE db.table`
   解析目标；解析不出来会优雅跳过并提示你显式传 `target_table`。
4. **分区只用 `pt_d`**。INSERT 里写了字面日期分区时，工具沿用它的**日期**，
   并统一归一化成天分区 `pt_d = 'YYYYMMDD'` —— 小时/分钟分区（`pt_h` / `pt_i`）
   不单独使用（与数据探查、质量校验链路的 `pt_d` 口径一致）。
   写的是 `$date` 占位符时，工具用默认分区列 `pt_d` + 一周前的日期。
   要查别的分区，显式传 `partition_filter`（同样只支持 `pt_d`）。
5. **采样而非全表**。默认 `LIMIT 10000` 行统计，极端倾斜数据请调大
   `tools.yaml` 里 `dataops_validate_data.config.quality_check.sample_size`。
6. **三条 SQL 并发执行**。DataOps 不支持把多个校验项合并成一条语句，所以
   空值率 / 单一值比率 / 异常值占比是**三条独立语句并发提交**的。

## 返回结果解读

返回值里 `data` 部分：

| 字段 | 含义 |
|---|---|
| `ok` | 是否全部通过（无告警） |
| `skipped` | 是否被优雅跳过（没 SQL / 不是 INSERT / 解析不出目标表） |
| `reason` | 跳过原因：`no_sql` / `not_insert_sql` / `no_target_table` / `no_checkable_fields` |
| `warnings` | 告警列表（人类可读） |
| `details` | `{字段名: {null_rate, single_value_ratio, outlier_ratio}}` |
| `markdown` | 渲染好的 Markdown 表格 + 结论（前端展示用） |

`frontend_msg` 里就是那张 Markdown 表格：

```
| 字段名 | 非空率(覆盖率) | 单一值比率 | 异常值占比 | 状态 |
|---|---|---|---|---|
| udid | 1.0000 | 0.0120 | — | ✅ 通过 |
| app_dl_retention_ratio_1d_udid | 0.4500 | 0.9800 | 0.0500 | ⚠️ 告警 |
```

`—` 表示该指标**不适用**（例如非比率类字段不参与异常值占比）。

## 扩展：新增第 N 种校验

在 `actions/tools/local_tool/dataops_quality_check.py` 的 `_BUILTIN_CHECKERS`
追加一个 `QualityChecker`，并在 `tools.yaml` 的
`dataops_validate_data.config.quality_check.checks` 里按名称启用。**编排层无需改动**。

## 相关文档

- 试算 + 交付后自动校验：`dataops_quality_check` skill
- 字段存在性/取值范围探查：`dataops_probe` skill
- SQL 语法/规范校验：`validate_deliverables` 工具
