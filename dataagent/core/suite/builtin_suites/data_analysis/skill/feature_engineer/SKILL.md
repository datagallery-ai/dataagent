---
name: feature-engineer
description: >-
  执行 step2_0→step2_5 标准特征工程流水线，产出训练宽表。
  Use when 采样完成后。
disable-model-invocation: true
---

# Feature Engineering Pipeline（step2_0 → step2_5）

输入：采样阶段传来的 `step1_output_meta.json` + 全部 `step1_sampled_*` ClickHouse 表。
输出：`step2_4_wide_userfiltered.csv`（模型唯一训练数据） + `receipt.json`。

## 数据操作规则

| 步骤 | ClickHouse 访问方式 | 本地文件 |
|------|-------------------|---------|
| **step2_0 ~ step2_3** | 仅 `submit_resource_job` / `poll_job` / `cancel_job` / `collect_job`<br>**严禁 Bash 连接 ClickHouse**<br>**严禁导出中间 CSV**（所有表数据必须留在 CH）<br>长 SQL 用 `command_file` 提交，禁止直接传 `command` | 仅 `.md`、`.json` 分析产物 |
| **step2_4** | 同上执行 SQL 建表和校验<br>**校验通过后**允许 Bash 连接 CH 导出最终宽表 | `.json` + **`step2_4_wide_userfiltered.csv`** |

**表结构与字段含义**：通过 step2_0 的数据画像直接从 ClickHouse 分析源数据推断，列角色初始信息来自 `step1_output_meta.json` 中的表类型。不依赖外部语义检索服务。

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
| step2_0 | `step2_0_table_profile`, `step2_0_column_profile` | `schema_resolution.json`, **`step2_0_source_data_analyze.md`** |
| step2_1 | `step2_1_wide_simple` | — |
| step2_2 | `step2_2_cleaning_report`, `step2_2_wide_cleaned` | `step2_2_cleaning_report.json` |
| step2_3 | `step2_3_wide_complete` | `step2_3_feature_derivation.md`, `step2_3_high_cardinality_check.json` |
| step2_4 | `step2_4_wide_userfiltered` | `step2_4_user_filter_report.json` + **`step2_4_wide_userfiltered.csv`** |
| step2_5 | — | **`receipt.json`** |

SQL 执行顺序：

```text
step2_0 阶段1: 构建 schema_resolution.json（骨架）
step2_0 阶段2: 替换占位符后执行
  scripts/step2_0_source_data_analyze.sql
  scripts/step2_0_column_profile.sql
  scripts/step2_0_validation.sql
step2_0 阶段3: 补全 schema_resolution.json + 撰写 step2_0_source_data_analyze.md
scripts/step2_1_simple_merge.sql
scripts/step2_1_validation.sql
scripts/step2_2_cleaning_report.sql
scripts/step2_2_feature_cleaning.sql
scripts/step2_2_validation.sql
scripts/step2_3_feature_aggregation.sql
scripts/step2_3_validation.sql
scripts/step2_4_user_cleaning.sql
scripts/step2_4_validation.sql
step2_5 → scripts/step2_5_finalize.md → receipt.json
```

每文件一条 SQL，建表 `CREATE OR REPLACE TABLE ... AS SELECT`，校验 `SELECT`。
`/*__...__*/` 动态块按 `schema_resolution` 和画像结果展开后提交。`{{output_database}}` 和 `<...>` 占位符必须全部替换。

**阶段依赖关系**：
- `schema_resolution.json` 在 step2_0 中经历 **骨架 → 画像 → 补全** 三个子阶段。
- 骨架版只包含可确定角色（`<user_table>`、`<user_id>`、`<label>`），用于替换画像 SQL 模板中的占位符。
- 画像完成后，用结果补全 `<age>`、`<gender>`、`<game_id>` 等需值分布确认的角色，形成最终版。
- 后续 step2_1 ~ step2_5 使用最终版 `schema_resolution.json`。

---

## step2_0：源数据深入理解

本步是采样阶段之后的核心分析步，分三个阶段执行：

### 阶段 1：构建 schema_resolution.json（骨架）

在 job workspace 创建 **`schema_resolution.json` 骨架版**。此阶段**不连线**，仅用 `step1_output_meta.json` 静态推断：

1. **`output_database`** ← `output_meta.output_database`
2. **`source_mode`** ← `"sampled"`
3. **`source_tables`** ← `output_meta.projection_tables[].table`
4. **`roles`**（骨架，仅填可确定性推断的角色）：
   - `<user_table>` → `projection_tables` 中 `type == "user_table"` 的那张表
   - `<user_id>` → **直接取 `user_table` 的 `<user_id>` 列名**。`rank_flg`、`dsid` 是等价映射键，也作为关联键使用，但 `<user_id>` 占位符必须填入实际的 `user_id` 列名（根据 `output_meta.projection_tables[].type` 中 `user_table` 的表结构获取）
   - `<label>` → 固定列名 `label`，位于 `<user_table>` 中
   - 其余角色（`<age>`、`<gender>`、`<game_id>` 等）暂留 `<TBD>` 占位，等阶段 2 画像完成后补全

必须在此阶段完成**候选键验证**，通过 ClickHouse MCP `submit_resource_job` 提交临时 `SELECT` 查询（不走 Bash），结果写入 `schema_resolution.json` 的 `key_validation` 字段：

- 候选键检查空值率、唯一性、重复倍数，不可仅凭字段名声明主键
  - 输出字段：`column`、`null_rate`、`uniqueness`、`max_duplication_factor`、`validated`
- `source_tables` 与 `output_meta.projection_tables` 逐一核对，禁止混入原始源表或 `step1_temp_*`
  - 输出字段：`source_tables_matched`

骨架版完成后，用 `roles` 中的 `<user_table>`、`<user_id>`、`<label>` 替换 `step2_0_*.sql` 模板中的对应占位符（`{{output_database}}` 也一并替换），其余 `<TBD>` 角色可以不改（三个画像 SQL 不引用它们）。

### 阶段 2：执行源数据画像 SQL

将骨架版 schema_resolution 中已确定的占位符替换到画像模板后，执行：

`step2_0_source_data_analyze.sql` → `step2_0_column_profile.sql` → `step2_0_validation.sql`

`step2_0_validation.sql` 的 `collect_job` 结果（包含 label 的空值/0/1/正负样本等统计）必须写回 `schema_resolution.json` 的 `key_validation.label_verified` 字段：
  - 输出字段：`label_validated`、`label_null_count`、`label_values`、`label_positive_count`、`label_negative_count`

### 阶段 3：补全 schema_resolution.json 并撰写分析文档

根据画像结果，补全 `schema_resolution.json` 中 `<TBD>` 的角色：
   - `<age>`、`<gender>` → 在 `<user_table>` 的列画像中按值分布推断（如数值范围验证年龄，枚举值验证性别，并记录合法值域）
   - `<game_id>` → 在 `game_keyed` 表的列画像中按列名约定推断
   - 其余角色从数据画像中按列名约定和值分布推断，不可仅凭列名字面声明

然后从 ClickHouse 画像结果整理生成 **`step2_0_source_data_analyze.md`**。

#### 表分类

| 分类 | 定义 | 处理策略 |
|------|------|---------|
| **1:1 表** | 以 `<user_id>` 为键，每用户最多一行 | 重复时先查明原因，不可直接去重 |
| **1:N 表** | 同一用户有多条事件/行为/订单/时序记录 | step2_3 聚合处理 |
| **游戏维度表** | 以 `<game_id>` 为键描述游戏属性 | step2_3 连接 |
| **未使用** | 无可关联键或与目标无关 | 记录原因 |

分类完成后检查是否有遗漏的源表，确认总数 = `source_tables` 数量。

**键映射规则**：`rank_flg`、`dsid` 均视为 `<user_id>` 的等价映射键。在分类时，这些列与实际 `<user_id>` 列同等对待，均作为关联键使用。

#### 字段画像

对每张表记录：业务含义、粒度、主键/候选键、行数、用户数、分类。

对每个字段记录：

1. 原始字段名、英文标准名、数据类型、主键标记
2. **业务含义**（通过字段名、值的分布和样本值推断）
3. 特征值语义（编码含义、取值说明）
4. `n_unique`、空值数、空值率
5. 至少三个互不相同的非空示例；`n_unique ≤ 10` 时列出全部取值及频数
6. 常量判断：
   - 仅一个非空值 → 常量候选
   - 不同文案表达同一状态（如"预约完成"和"已预约"语义完全一致）→ 语义常量（标记 `semantic_constant: true`）
7. **合法值域与异常值**：对 `<age>`、`<gender>` 等保护字段，记录合法取值范围（如 age 在 1-120 为合法，0、负数、空为非法；gender 合法枚举集合），供 step2_4 用户过滤使用
8. **聚合方式建议**（仅 1:N 表字段）：对 1:N 表的每个数值/时间/类别字段，给出推荐聚合方式及理由

   | 字段类型 | 推荐聚合 |
   |---------|---------|
   | 金额/耗时/次数 | sum、avg |
   | 状态/类型（枚举） | count、mode（最近值） |
   | 时间戳 | min、max、datediff（距今） |
   | ID 字段 | countDistinct |

---

## step2_1：1:1 初步合表

执行 `step2_1_simple_merge.sql` → `step2_1_validation.sql`。

以 `<user_table>` 为基表，所有 1:1 表按 `<user_id>` 左连接。连接前验证右表键唯一，连接后验证行数和用户数未膨胀。重名字段加来源表前缀。

---

## step2_2：原始特征清洗

执行 `step2_2_cleaning_report.sql` → `step2_2_feature_cleaning.sql` → `step2_2_validation.sql`。

> **与 step2_0 列画像的区别**：step2_0 的 `column_profile` 是对**原始源表**做探索性画像（理解数据），step2_2 的 `cleaning_report` 是对**合表后的 `step2_1_wide_simple`** 做清洗决策画像。合表后字段名可能因重名加前缀，字段数量和内容已不同。两者目的不同，不可跳过。

- **`step2_2_cleaning_report.sql` 的 `/*__COLUMN_PROFILE_SELECTS__*/` 需展开为多条 UNION ALL SELECT，展开后 SQL 很长。** 必须先将完整展开后的 SQL 通过 bash/Python `write_file` 写入 workspace 文件，再以 `submit_resource_job(command_file="step2_2_cleaning_report.sql")` 一次性提交。

**直接引用 step2_0 字段画像中的常量判断结果**：对于 step2_0 中已标记 `semantic_constant: true` 的字段，直接纳入删除决策，无需在 step2_2 重新判断语义等价性。

依据 step2_0 画像逐字段决策：

| 条件 | 动作 |
|------|------|
| `missing_rate > 0.5` | 删除 |
| 仅"空值 + 一个有效值"或 `n_unique == 1` | 常量删除 |
| step2_0 中已标记 `semantic_constant: true` | 语义常量删除 |
| `<user_id>`、`<label>`、`<age>`、`<gender>` | **保护，不删除** |
| 其他 | 保留 |

生成的 `step2_2_cleaning_report.json` 每个字段含：
`feature`、`cleaned`、`recommendation`、`reason_code`、`reason`、`missing_rate`、`n_unique`、`semantic_constant`。
`reason_code` 使用枚举：`MISSING_RATE_GT_0_5` / `CONSTANT` / `SEMANTIC_CONSTANT`。

---

## step2_3：复杂聚合与特征衍生

执行 `step2_3_feature_aggregation.sql` → `step2_3_validation.sql`。
按全部 1:N、时序、游戏维表展开 `DERIVATION_CTES`、`DERIVED_SELECT_COLUMNS`、`DERIVATION_JOIN_BLOCKS`。

### 1:N 聚合

- 先明确事件粒度、时间窗、聚合键、去重规则，再按 `<user_id>` 聚合
- **聚合方式依据 step2_0 字段画像中记录的"聚合方式建议"**，不凭空推断。若无建议记录，按字段类型默认规则：数值金额类用 sum/avg，时间戳用 min/max/datediff，ID 类用 countDistinct，枚举类用 count/mode
- 原始缺失保留 NULL，不以 0/均值/众数/空字符串填充

### 高基数与列表特征处理

| 场景 | 处理方式 |
|------|---------|
| 数值字段 `n_unique > 100` | 分位点或业务阈值分箱 → 新字段 → 删除旧字段 |
| 城市 | `<city_tier_map>` 级联映射：一二线标准层级，其他非空统一三线，缺失 NULL |
| 字符串字段 | 依据 step2_0 值语义分箱，禁止散列编码 |
| 列表字段（`#`/`^` 分隔） | 词项 ≤100：生成二元特征 + 列表长度 + 是否为空 → 删除原字段。词项 >100：业务归类或 Top + other |

收尾门禁：除 `<user_id>` 等标识列外，不得残留 `n_unique > 100` 字段。所有特征名英文 snake_case。

高基数门禁通过后，写入 **`step2_3_high_cardinality_check.json`**，每个被检查的字段含：

| 字段 | 类型 | 说明 |
|------|------|------|
| `feature` | string | 特征名 |
| `n_unique_before` | int | 处理前唯一值数 |
| `n_unique_after` | int | 处理后唯一值数 |
| `method` | string | 处理方式（`binning` / `city_tier_mapping` / `binary_encoding` / `top_and_other` / `none`） |
| `status` | string | `passed`（门禁通过） / `dropped`（已删除） |
| `reason` | string | 处理原因说明 |

### 特征文档

`step2_3_feature_derivation.md` 覆盖**所有**特征（最终保留 + 删除 + 新衍生）。每个记录：

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

## step2_4：用户过滤

执行 `step2_4_user_cleaning.sql` → `step2_4_validation.sql`。

- 删除年龄或性别不合法的用户：依据 step2_0 字段画像中记录的 `<age>`、`<gender>` 合法值域判定（如 age 为 0、负数、空视为非法；gender 不在合法枚举集合内视为非法）
- 门禁验证：`<user_id>` 唯一、`<label>` 仍为 0/1
- 记录过滤前后用户数、正负样本数及比例变化

写入 **`step2_4_user_filter_report.json`**，格式如下：

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

- 门禁通过后导出 `step2_4_wide_userfiltered.csv`

---

## step2_5：定稿 receipt

按 `scripts/step2_5_finalize.md` 验收并写 `receipt.json`。本步不写 SQL、不连 CH。

receipt 仅登记 Model 下游所需：`schema_resolution.json` + `step2_4_wide_userfiltered.csv`。

---

## 完成自检清单

提交 receipt 前逐项确认 pipeline 完整性：

- [ ] `schema_resolution.json` 已分阶段补全，`source_tables` 与 `output_meta.projection_tables` 一致
- [ ] step2_0 key_validation 中候选键验证和 label 验证已通过
- [ ] `step2_0_table_profile`、`step2_0_column_profile` 已创建且非空
- [ ] **`step2_0_source_data_analyze.md`** 已写入 workspace
- [ ] `step2_1_wide_simple` 已创建，连接验证通过
- [ ] `step2_2_cleaning_report`、`step2_2_wide_cleaned` 已创建，`step2_2_cleaning_report.json` 已写入
- [ ] `step2_3_wide_complete` 已创建，高基数门禁通过，`step2_3_feature_derivation.md` 和 `step2_3_high_cardinality_check.json` 已写入
- [ ] `step2_4_wide_userfiltered` 已创建，`<label>` 为 0/1，`step2_4_user_filter_report.json` 已写入
- [ ] **`step2_4_wide_userfiltered.csv`** 已导出
- [ ] 无 `_tmp_*`、`_ft_*`、`fe_` 等非标准前缀残留
