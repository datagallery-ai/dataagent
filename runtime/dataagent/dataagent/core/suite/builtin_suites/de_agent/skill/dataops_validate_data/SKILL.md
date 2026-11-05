---
name: dataops_validate_data
description: |
  Use when (任一即触发):
    1. 独立数据验证 —— 用户给了一张表（或一条 INSERT 语句 / 建表 DDL）,
       要看这张表/这批数据是否合理(空值/单一值/比率越界/行数);
    2. 目标表已有历史产出 —— 想直接对那张真实表做数据体检,而不是再跑一遍试算。
  本 skill **只做数据验证**:不建表、不插入、不用临时表替换,直接查真实目标表。
  字段来源三选一:①只给表名 → 走 semantic 元数据取列;②给 DDL → 解析列清单;
    ③给 DML(INSERT) → 解析 SELECT 投影。
  能力:空值覆盖率(non-null rate) + 单一值覆盖率(single-value ratio)
    + 异常值占比(outlier ratio,仅比率类字段) + 数据行数(row count,表级)。
  实现:工具 `dataops_validate_data`(`de_agent/tools/dataops_validate_data_tool.py`),
    校验器注册表在 `actions/tools/local_tool/dataops_quality_check.py`。
  NOT for: 建表 + 试算 + 交付后自动校验(那是 dataops_quality_check skill 的活);
    字段是否存在/取值范围探查(那是 dataops_probe 的活);
    SQL 语法/规范校验(那是 validate_deliverables 的活)。
---

# 独立数据验证 (Standalone Data Validation)

## 核心心智模型

```
   目标表 db.table  +  字段清单
        │              └─ 从哪来（三种给法，三条来源）
        │                 ① 表名 → semantic-service 列元数据
        │                 ② DDL  → CREATE TABLE 的列清单
        │                 ③ DML  → INSERT 的 SELECT 投影
        ▼
   直接查【真实目标表】（只读！）
        │
        ├─ null_rate          空值覆盖率
        ├─ single_value_ratio 单一值覆盖率
        ├─ outlier_ratio      异常值占比（仅比率类字段）
        └─ row_count          数据行数（表级指标，不挂字段）
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

- 用户直接甩了一张表名：「`biads.ads_user_profile_ds` 这张表的数据看着对吗？」
- 用户给了一条 INSERT：「这条 SQL 写出来的数据看着对吗？」
- 用户给了一段建表 DDL：「按这个表结构体检一下。」
- 目标表里已经有历史产出，想直接体检，不用再跑一遍试算。
- 交付前自查：想让 agent 主动对某个目标表跑一次数据体检。

**不该用的场景**：要「建表 + 试算 + 校验 + 清理」这一整套 —— 那是
`dataops_quality_check` skill 的 `trial_compute_table`（或交付后的 post-hook）。

## 典型调用

```python
# ① 只给表名 —— 字段走 semantic 元数据
dataops_validate_data(target_table="biads.ads_user_profile_ds")

# ② 给 DDL —— 字段从 CREATE TABLE 的列清单解析
dataops_validate_data(
    sql="CREATE TABLE biads.ads_user_profile_ds (udid STRING, pay_cnt_udid BIGINT)",
)

# ③ 给 DML —— 字段从 INSERT 的 SELECT 投影解析（推荐传文件路径，
#    避免大段 SQL 进 chat history）
dataops_validate_data(
    sql_file_path="<workspace>/sql/insert_ads_user_profile_ds.sql",
)
```

参数：

| 参数 | 必填 | 说明 |
|---|---|---|
| `sql` | 三选一 | SQL 语句文本（INSERT 或 DDL 皆可） |
| `sql_file_path` | 三选一 | 工作区里 SQL 文件的路径（**推荐**） |
| `target_table` | 三选一 | 目标表 `db.table`；留空则从 SQL 里解析 |
| `partition_filter` | 否 | 分区条件，如 `pt_d = '20261020'`；留空则从 INSERT 解析 |

> 至少要给出「表名 / SQL 文件路径 / SQL 文本」三者之一，否则工具会优雅跳过
> （`reason=no_sql`）。

## 字段来源优先级

| 给法 | 字段怎么来 | 有类型吗 |
|---|---|---|
| 表名 | semantic-service `table-columns-info` | 有（`value_type`） |
| DDL | `CREATE TABLE ... (col TYPE, ...)` | 有 |
| DML | `INSERT ... SELECT` 的投影别名 | semantic 补 → workspace 的 DDL → `IS NOT NULL` |

> DDL 的表名可以不带库名（`CREATE TABLE ads_x (...)`）—— 这种情况下要显式传
> `target_table`，工具才知道查哪张表。

字段类型决定判空写法：**STRING 存储**用 `!bicoredata.IsEmpty(col)`（空字符串也算空），
数值/时间戳等用 `col IS NOT NULL`。DDL / semantic 两条路径天然带类型，所以口径最准。

> `varchar(n)` / `char(n)` 也按字符串处理（空串算空）。类型查找**大小写不敏感** ——
> DDL / semantic 的列名大小写与 INSERT 投影不一致时也能对上。

分区列（`pt_d`/`pt`/`pt_h`）、时间戳（`create_time`/`update_time`/`modify_time`）、
主键 ID（`id`/`uuid`/`udid`/`guid`/`row_key`）会被自动排除 —— 这些字段做覆盖率统计没有业务含义。

**类型拿不到时的降级链**：semantic → workspace 的 `create_*.sql`（**仅当它的表名与目标表一致**）→
`IS NOT NULL`（对所有类型都成立，比用错类型安全）。

## 关键约束（Must Follow）

1. **只读**。本工具不建表、不插入、不删表、不改真实数据。它只是查表。
   不要在调用前先「建一张临时表再验证」—— 那是另一条链路。
2. **不强制要求 INSERT**。`CREATE` / `SELECT` / 空语句不会因为「不是 INSERT」被跳过 ——
   DDL 能提供字段，SELECT 会被优雅跳过（没有目标表可查）。
3. **目标表必须是真实表**。工具从 INSERT 的 `INSERT OVERWRITE TABLE db.table`
   或 DDL 的 `CREATE TABLE db.table` 解析目标；解析不出来会优雅跳过并提示显式传
   `target_table`。
4. **分区只用 `pt_d`**。INSERT 里写了字面日期分区时，工具沿用它的**日期**，
   并统一归一化成天分区 `pt_d = 'YYYYMMDD'` —— 小时/分钟分区（`pt_h` / `pt_i`）
   不单独使用（与数据探查、质量校验链路的 `pt_d` 口径一致）。
   写的是 `$date` 占位符时，工具用默认分区列 `pt_d` + 一周前的日期。
   要查别的分区，显式传 `partition_filter`（同样只支持 `pt_d`）。
5. **采样而非全表**。默认 `LIMIT 10000` 行统计，极端倾斜数据请调大
   `tools.yaml` 里 `dataops_validate_data.config.quality_check.sample_size`。
   （例外：数据行数 `row_count` 是表级指标，必须全量 COUNT，不采样。）
6. **多条 SQL 并发执行**。DataOps 不支持把多个校验项合并成一条语句，所以
   空值率 / 单一值比率 / 异常值占比 / 数据行数是**多条独立语句并发提交**的。

## 返回结果解读

返回值里 `data` 部分：

| 字段 | 含义 |
|---|---|
| `ok` | 是否全部通过（无告警） |
| `skipped` | 是否被优雅跳过（没输入 / 解析不出目标表 / 拿不到字段） |
| `reason` | 跳过原因：`no_sql` / `no_target_table` / `no_checkable_fields` / `schema_unavailable` / `check_not_run` |
| `field_source` | 字段来源：`ddl` / `dml` / `semantic` |
| `warnings` | 告警列表（人类可读） |
| `details` | `{字段名: {null_rate, single_value_ratio, outlier_ratio}}` |
| `table_metrics` | `{row_count: N}` —— **表级**指标（数据行数），不挂字段 |
| `markdown` | 渲染好的 Markdown 表格 + 结论（前端展示用） |

`frontend_msg` 里就是那张 Markdown 表格：

```
| 字段名 | 非空率(覆盖率) | 单一值比率 | 异常值占比 | 状态 |
|---|---|---|---|---|
| udid | 1.0000 | 0.0120 | — | ✅ 通过 |
| app_dl_retention_ratio_1d_udid | 0.4500 | 0.9800 | 0.0500 | ⚠️ 告警 |
```

`—` 表示该指标**不适用**（例如非比率类字段不参与异常值占比）。

表级指标单独一张表：

```
| 表级指标 | 值 |
|---|---|
| 数据行数 | 12345 |
```

## 扩展：新增第 N 种校验

在 `actions/tools/local_tool/dataops_quality_check.py` 的 `_BUILTIN_CHECKERS`
追加一个 `QualityChecker`，并在 `tools.yaml` 的
`dataops_validate_data.config.quality_check.checks` 里按名称启用。**编排层无需改动**。

## 相关文档

- 试算 + 交付后自动校验：`dataops_quality_check` skill
- 字段存在性/取值范围探查：`dataops_probe` skill
- SQL 语法/规范校验：`validate_deliverables` 工具
