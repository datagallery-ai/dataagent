---
name: feature-engineer
description: >-
  执行 step2_0→step2_6 标准特征工程流水线，产出训练宽表。
  Use when 采样完成后。
disable-model-invocation: true
---

# Feature Engineering Pipeline（step2_0 → step2_6）

输入：采样阶段传来的 `step1_output_meta.json` + 全部 `step1_sampled_*` ClickHouse 表。
输出：`step2_5_wide_userfiltered.csv`（模型唯一训练数据） + `receipt.json`。

## 数据操作规则

| 步骤 | ClickHouse 访问方式 | 本地文件 |
|------|-------------------|---------|
| **step2_0 ~ step2_4** | 仅 `submit_resource_job` / `poll_job` / `cancel_job` / `collect_job`<br>**严禁 Bash 连接 ClickHouse**<br>**严禁导出中间 CSV**（所有表数据必须留在 CH）<br>长 SQL 用 `command_file` 提交，禁止直接传 `command` | 仅 `.md`、`.json` 分析产物 |
| **step2_5** | 同上执行 SQL 建表和校验<br>**校验通过后**允许 Bash 连接 CH 导出最终宽表 | `.json` + **`step2_5_wide_userfiltered.csv`** |

**语义查询**：通过 `semantic_retrieve` MCP 获取表结构、字段含义和 schema 角色，禁止自行连接数据源做语义解析。

## 工具使用规则

- `submit_resource_job` 通过 `command` 或 `command_file` 传递 SQL：
  - `command` 适合短语句（约 2000 字符以内），直接传入 SQL 文本。
  - **长 SQL（如展开超过 2000 字符的 `/*__...__*/` 动态块）必须先 `write_file` 写入 workspace**，
    再通过 `command_file="your_file.sql"` 提交。禁止直接将超长 SQL 作为 `command` 参数传入，
    也禁止将 SQL 拆分多次提交（会导致建表不完整）。
  - `command` 和 `command_file` 互斥，只能选其一。

**脚本路径映射**：本文档中所有 `scripts/` 前缀指向 SKILL 包内的 `skill/feature-engineer/scripts/` 目录，不要假定工作区根目录下存在 `scripts/` 目录。
---

## Pipeline 总览

| 步骤 | ClickHouse 表 | Workspace 文档 |
|------|---------------|----------------|
| step2_0 | — | `schema_resolution.json` |
| step2_1 | `step2_1_table_profile`, `step2_1_column_profile` | **`step2_1_source_data_analyze.md`** |
| step2_2 | `step2_2_wide_simple` | — |
| step2_3 | `step2_3_cleaning_report`, `step2_3_wide_cleaned` | `step2_3_cleaning_report.json` |
| step2_4 | `step2_4_wide_complete` | `step2_4_feature_derivation.md`, `step2_4_high_cardinality_check.json` |
| step2_5 | `step2_5_wide_userfiltered` | `step2_5_user_filter_report.json` + **`step2_5_wide_userfiltered.csv`** |
| step2_6 | — | **`receipt.json`** |

SQL 执行顺序：

```text
step2_0 → schema_resolution.json
scripts/step2_1_source_data_analyze.sql
scripts/step2_1_column_profile.sql
scripts/step2_1_validation.sql
scripts/step2_2_simple_merge.sql
scripts/step2_2_validation.sql
scripts/step2_3_cleaning_report.sql
scripts/step2_3_feature_cleaning.sql
scripts/step2_3_validation.sql
scripts/step2_4_feature_aggregation.sql
scripts/step2_4_validation.sql
scripts/step2_5_user_cleaning.sql
scripts/step2_5_validation.sql
step2_6 → scripts/step2_6_finalize.md → receipt.json
```

每文件一条 SQL，建表 `CREATE OR REPLACE TABLE ... AS SELECT`，校验 `SELECT`。
`/*__...__*/` 动态块按 `schema_resolution` 和画像结果展开后提交。`{{database}}` 和 `<...>` 占位符必须全部替换。

---

## step2_0：构建 schema_resolution.json

在 job workspace 创建 **`schema_resolution.json`**：

1. **`database`** ← `output_meta.database`
2. **`source_mode`** ← `"sampled"`
3. **`source_tables`** ← `output_meta.projection_tables[].table`
4. **`roles`** ← 结合 `semantic_retrieve` 与 `output_meta.projection_tables[].type`：
   - `<user_table>` → `type == "user_table"`
   - `<user_id>` → 语义层确认的用户键（`rank_flg`、`dsid` 均映射到 `<user_id>`）
   - `<label>` → 固定列 `label`
   - 其余角色（`<city>`、`<age>`、`<gender>`、`<game_id>` 等）由语义层 + 列画像确认

门禁：

以下验证须通过 ClickHouse MCP `submit_resource_job` 提交临时 `SELECT` 查询完成（不走 Bash），结果写入 `schema_resolution.json` 的 `key_validation` 字段：

- 候选键须检查空值率、唯一性、重复倍数，不可仅凭字段名声明主键
  - 输出字段：`column`、`null_rate`、`uniqueness`、`max_duplication_factor`、`validated`
- `source_tables` 与 `output_meta.projection_tables` 逐一核对，禁止混入原始源表或 `step1_temp_*`
  - 输出字段：`source_tables_matched`
- 在 `<user_table>` 上验证 `<label>` 非空、仅含 0/1、正负类均存在
  - 输出字段：`label_validated`、`label_null_count`、`label_values`、`label_positive_count`、`label_negative_count`

---

## step2_1：源数据分析

执行 `step2_1_source_data_analyze.sql` → `step2_1_column_profile.sql` → `step2_1_validation.sql`。
门禁通过后，从 ClickHouse 查询结果整理生成 **`step2_1_source_data_analyze.md`**。

### 表分类

| 分类 | 定义 | 处理策略 |
|------|------|---------|
| **1:1 表** | 以 `<user_id>` 为键，每用户最多一行 | 重复时先查明原因，不可直接去重 |
| **1:N 表** | 同一用户有多条事件/行为/订单/时序记录 | step2_4 聚合处理 |
| **游戏维度表** | 以 `<game_id>` 为键描述游戏属性 | step2_4 连接 |
| **未使用** | 无可关联键或与目标无关 | 记录原因 |

分类完成后检查是否有遗漏的源表，确认总数 = `source_tables` 数量。

**键映射规则**：`rank_flg`、`dsid` 均视为 `<user_id>` 的等价映射键。在分类时，这些列与实际 `<user_id>` 列同等对待，均作为关联键使用。

### 字段画像

对每张表记录：业务含义、粒度、主键/候选键、行数、用户数、分类。

对每个字段记录：

1. 原始字段名、英文标准名、数据类型、主键标记
2. **业务含义**（通过 `semantic_retrieve` MCP 获取）
3. 特征值语义（编码含义、取值说明）
4. `n_unique`、空值数、空值率
5. 至少三个互不相同的非空示例；`n_unique ≤ 10` 时列出全部取值及频数
6. 常量判断：
   - 仅一个非空值 → 常量候选
   - 不同文案表达同一状态（如"预约完成"和"已预约"语义完全一致）→ 常量特征

---

## step2_2：1:1 初步合表

执行 `step2_2_simple_merge.sql` → `step2_2_validation.sql`。

以 `<user_table>` 为基表，所有 1:1 表按 `<user_id>` 左连接。连接前验证右表键唯一，连接后验证行数和用户数未膨胀。重名字段加来源表前缀。

---

## step2_3：原始特征清洗

执行 `step2_3_cleaning_report.sql` → `step2_3_feature_cleaning.sql` → `step2_3_validation.sql`。

- **`step2_3_cleaning_report.sql` 的 `/*__COLUMN_PROFILE_SELECTS__*/` 需展开为多条 UNION ALL SELECT，展开后 SQL 很长。** 必须先将完整展开后的 SQL 通过 bash/Python `write_file` 写入 workspace 文件，再以 `submit_resource_job(command_file="step2_3_cleaning_report.sql")` 一次性提交。

依据 step2_1 画像逐字段决策：

| 条件 | 动作 |
|------|------|
| `missing_rate > 0.5` | 删除 |
| 仅"空值 + 一个有效值"或 `n_unique == 1` | 常量删除 |
| 多个取值语义完全一致 | 语义常量删除 |
| `<user_id>`、`<label>`、`<age>`、`<gender>` | **保护，不删除** |
| 其他 | 保留 |

生成的 `step2_3_cleaning_report.json` 每个字段含：
`feature`、`cleaned`、`recommendation`、`reason_code`、`reason`、`missing_rate`、`n_unique`、`semantic_constant`。
`reason_code` 使用枚举：`MISSING_RATE_GT_0_5` / `CONSTANT` / `SEMANTIC_CONSTANT`。

---

## step2_4：复杂聚合与特征衍生

执行 `step2_4_feature_aggregation.sql` → `step2_4_validation.sql`。
按全部 1:N、时序、游戏维表展开 `DERIVATION_CTES`、`DERIVED_SELECT_COLUMNS`、`DERIVATION_JOIN_BLOCKS`。

### 1:N 聚合

- 先明确事件粒度、时间窗、聚合键、去重规则，再按 `<user_id>` 聚合
- 根据语义选择 count / 去重 count / sum / avg / min / max / 最近值 / 时间差，禁止无依据全量套用
- 原始缺失保留 NULL，不以 0/均值/众数/空字符串填充

### 高基数与列表特征处理

| 场景 | 处理方式 |
|------|---------|
| 数值字段 `n_unique > 100` | 分位点或业务阈值分箱 → 新字段 → 删除旧字段 |
| 城市 | `<city_tier_map>` 级联映射：一二线标准层级，其他非空统一三线，缺失 NULL |
| 字符串字段 | 依据 step2_1 值语义分箱，禁止散列编码 |
| 列表字段（`#`/`^` 分隔） | 词项 ≤100：生成二元特征 + 列表长度 + 是否为空 → 删除原字段。词项 >100：业务归类或 Top + other |

收尾门禁：除 `<user_id>` 等标识列外，不得残留 `n_unique > 100` 字段。所有特征名英文 snake_case。

高基数门禁通过后，写入 **`step2_4_high_cardinality_check.json`**，每个被检查的字段含：

| 字段 | 类型 | 说明 |
|------|------|------|
| `feature` | string | 特征名 |
| `n_unique_before` | int | 处理前唯一值数 |
| `n_unique_after` | int | 处理后唯一值数 |
| `method` | string | 处理方式（`binning` / `city_tier_mapping` / `binary_encoding` / `top_and_other` / `none`） |
| `status` | string | `passed`（门禁通过） / `dropped`（已删除） |
| `reason` | string | 处理原因说明 |

### 特征文档

`step2_4_feature_derivation.md` 覆盖**所有**特征（最终保留 + 删除 + 新衍生）。每个记录：

| 字段 | 说明 |
|------|------|
| `status` | kept / dropped / derived |
| `method` | 处理方式（分箱/映射/聚合等） |
| `reason` | 处理原因 |
| `source_table` | 来源表 |
| `source_feature` | 来源字段 |
| `source_n_unique` | 处理前唯一值数 |
| `output_n_unique` | 处理后唯一值数 |
| 空值策略 | NULL 保留 / 填充 / 不适用 |
| SQL 表达式 / 连接方式 | 具体实现 |

新衍生特征还要说明阈值、词表或映射版本。

---

## step2_5：用户过滤

执行 `step2_5_user_cleaning.sql` → `step2_5_validation.sql`。

- 删除年龄或性别为空的用户（年龄 `0` 或非法值依据 step2_1 语义判断）
- 门禁验证：`<user_id>` 唯一、`<label>` 仍为 0/1
- 记录过滤前后用户数、正负样本数及比例变化

写入 **`step2_5_user_filter_report.json`**，格式如下：

```json
{
  "before": {
    "total_users": <int>,
    "positive_count": <int>,
    "negative_count": <int>,
    "positive_ratio": <float>
  },
  "after": {
    "total_users": <int>,
    "positive_count": <int>,
    "negative_count": <int>,
    "positive_ratio": <float>
  },
  "filter_reasons": ["<string>"]
}
```

- 门禁通过后导出 `step2_5_wide_userfiltered.csv`

---

## step2_6：定稿 receipt

按 `scripts/step2_6_finalize.md` 验收并写 `receipt.json`。本步不写 SQL、不连 CH。

receipt 仅登记 Model 下游所需：`schema_resolution.json` + `step2_5_wide_userfiltered.csv`。

---

## 完成自检清单

提交 receipt 前逐项确认 pipeline 完整性：

- [ ] `schema_resolution.json` 已创建，`source_tables` 与 `output_meta.projection_tables` 一致
- [ ] `step2_1_table_profile`、`step2_1_column_profile` 已创建且非空
- [ ] **`step2_1_source_data_analyze.md`** 已写入 workspace
- [ ] `step2_2_wide_simple` 已创建，连接验证通过
- [ ] `step2_3_cleaning_report`、`step2_3_wide_cleaned` 已创建，`step2_3_cleaning_report.json` 已写入
- [ ] `step2_4_wide_complete` 已创建，高基数门禁通过，`step2_4_feature_derivation.md` 和 `step2_4_high_cardinality_check.json` 已写入
- [ ] `step2_5_wide_userfiltered` 已创建，`<label>` 为 0/1，`step2_5_user_filter_report.json` 已写入
- [ ] **`step2_5_wide_userfiltered.csv`** 已导出
- [ ] 无 `_tmp_*`、`_ft_*`、`fe_` 等非标准前缀残留