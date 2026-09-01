---
name: feature-engineer
description: >-
  执行 step2_0→step2_5 标准特征工程流水线，产出训练宽表。
  Use when 采样完成后。
disable-model-invocation: true
---

# Feature Engineering Pipeline（step2_0 → step2_5）

输入：采样阶段传来的 `step1_output_meta.json` + 全部交付表（output_database 内与源表同名）。
输出：`step2_4_wide_userfiltered.csv`（模型唯一训练数据）以及原有分析产物；增量输出
`step2_3_deployment_feature_contract.json`，供 NL2SQL 在全量源表上确定性回放特征。

## 数据操作规则

| 步骤 | ClickHouse 访问方式 | 本地文件 |
|------|-------------------|---------|
| **step2_0 ~ step2_3** | 仅 `submit_resource_job` / `poll_job` / `cancel_job` / `collect_job`<br>**严禁 Bash 连接 ClickHouse**<br>**严禁导出中间 CSV**（所有表数据必须留在 CH）<br>长 SQL 用 `command_file` 提交，禁止直接传 `command` | 仅 `.md`、`.json` 分析产物 |
| **step2_4** | 建表和校验仍只用 MCP。CSV 导出见下方**导出方式优先级** | `.json` + **`step2_4_wide_userfiltered.csv`** |

**表结构与字段含义**：通过 step2_0 的数据画像直接从 ClickHouse 分析源数据推断，列角色初始信息来自 `step1_output_meta.json` 中的表类型。不依赖外部语义检索服务。

### CSV 导出方式优先级（step2_4 校验通过后）

顺序不可颠倒。细节与反面案例见 `scripts/step2_4_export_csv.md`（独立 todo，必须 `read_file`）。

1. **默认导出接口（必须先用）**：ClickHouse MCP 资源 `resource_id="clickhouse"` 的
   `submit_resource_job` 分批 `SELECT` → `poll_job` → `collect_job`，再用本地 Python
   把已收集的行写入 `step2_4_wide_userfiltered.csv`。**不要** `curl` 直连 `:8123`。
   MCP server 已持有 ClickHouse 凭据，Agent 侧不需要密码。
2. **首次认证失败立即切换**：若仍尝试了 HTTP/`curl`/驱动直连且返回 401/403/ClickHouse 516
   / Authentication failed，**立刻**回到第 1 条 MCP 方案。禁止继续直连，禁止花费时间搜索凭据。
3. **运行环境预置连接信息（仅直连兜底）**：直连只允许读取已注入的环境变量
   `CH_HOST` / `CH_PORT` / `CH_USER` / `CH_PASSWORD`（兼容 `CLICKHOUSE_*`）。
   `CH_PASSWORD` 未设置或为空 → 直连不可用，直接走第 1 条。
   凭据由本机 `.env` 或 `~/.bashrc` 注入进程环境，禁止写入代码或 Suite YAML，禁止扫描磁盘/进程/数据库。

## 工具使用规则

- `submit_resource_job` 通过 `command` 或 `command_file` 传递 SQL：
  - `command` 适合短语句（约 2000 字符以内），直接传入 SQL 文本。
  - **长 SQL（如展开超过 2000 字符的 `/*__...__*/` 动态块）必须先 `write_file` 写入 workspace**，
    再通过 `command_file="your_file.sql"` 提交。禁止直接将超长 SQL 作为 `command` 参数传入，
    也禁止将 SQL 拆分多次提交（会导致建表不完整）。
  - `command` 和 `command_file` 互斥，只能选其一。

**脚本路径映射**：本文档中所有 `scripts/` 前缀指向 SKILL 包内的 `skill/feature-engineer/scripts/` 目录，不要假定工作区根目录下存在 `scripts/` 目录。

## 进度标记（减少上下文切换）

每个大阶段以及 step2_3 每个 todo 完成时，立刻覆盖写入对应文件，便于中断后 `cat` 恢复，禁止凭对话记忆回忆「做到哪了」。

| 文件 | 何时写入 |
|------|----------|
| `progress_step2_0.txt` | step2_0 阶段 3 完成（含 `gate_data_verified.txt`） |
| `progress_step2_1.txt` | `step2_1_wide_simple` 校验通过 |
| `progress_step2_2.txt` | `step2_2_wide_cleaned` 与 cleaning_report 门禁通过 |
| `progress_step2_3.txt` | step2_3 **每个** todo 完成时更新（含 todo 序号） |
| `progress_step2_4.txt` | CSV 导出且契约检查跑完 |

格式（纯文本，四行）：

```text
status=done
todo=<序号或 all>
updated=<ISO-8601>
summary=<一句：已产出哪些表/文件>
```

进入任一步前先 `cat progress_step2_*.txt 2>/dev/null`；已 `status=done` 的阶段不要重做，从第一个未完成阶段继续。

---

## Pipeline 总览

| 步骤 | ClickHouse 表 | Workspace 文档 |
|------|---------------|----------------|
| step2_0 阶段1 | — | `schema_resolution.json`（骨架） |
| step2_0 阶段2 | `step2_0_table_profile`、`step2_0_column_profile` | — |
| **step2_0 阶段2.5** | — | **格式门禁 → `step2_0_source_data_analyze.md`** |
| step2_0 阶段3 | — | `schema_resolution.json`（补全） |
| step2_1 | `step2_1_wide_simple` | — |
| step2_2 | `step2_2_cleaning_report`, `step2_2_wide_cleaned` | `step2_2_cleaning_report.json` |
| step2_3 | `step2_3_wide_complete` | `step2_3_feature_derivation.md`, `step2_3_high_cardinality_check.json`, `step2_3_deployment_feature_contract.json`（新增） |
| step2_4 | `step2_4_wide_userfiltered` | `step2_4_user_filter_report.json` + **`step2_4_wide_userfiltered.csv`** |
| step2_5 | — | **`receipt.json`** |

SQL 执行顺序：

```text
step2_0 阶段1: 构建 schema_resolution.json（骨架）
step2_0 阶段2: 替换占位符后执行
  scripts/step2_0_source_data_analyze.sql
  scripts/step2_0_column_profile.sql
  scripts/step2_0_validation.sql
step2_0 阶段2.5: 格式门禁 — 执行 scripts/step2_0_format_gate.md
step2_0 阶段3: 补全 schema_resolution.json + 撰写 step2_0_source_data_analyze.md
scripts/step2_1_simple_merge.sql
scripts/step2_1_validation.sql
scripts/step2_2_format_gate.md    ← ⛔ 独立门禁 todo（不可跳过）
scripts/step2_2_cleaning_report.sql
scripts/step2_2_feature_cleaning.sql
scripts/step2_2_validation.sql
scripts/step2_3_feature_aggregation.sql
scripts/step2_3_format_gate.md    ← ⛔ 提交前硬门禁
scripts/step2_3_string_cast.md    ← ⛔ 残留 String 先采样再 CAST
scripts/step2_3_validation.sql
scripts/step2_4_user_cleaning.sql
scripts/step2_4_validation.sql
scripts/step2_4_export_csv.md    ← ⛔ 独立门禁 todo（MCP 优先导出，禁止凭据搜索）
导出 step2_4_wide_userfiltered.csv 后执行 scripts/step2_3_validate_deployment_contract.py
step2_5 → scripts/step2_5_finalize.md → receipt.json
```

每文件一条 SQL，建表 `CREATE OR REPLACE TABLE ... AS SELECT`，校验 `SELECT`。
`/*__...__*/` 动态块按 `schema_resolution` 和画像结果展开后提交。`{{output_database}}` 和 `<...>` 占位符必须全部替换。

**阶段依赖关系**：
- `schema_resolution.json` 在 step2_0 中经历 **骨架 → 画像 → 补全** 三个子阶段。
- 骨架版只包含可确定角色（`<user_table>`、`<user_id>`、`<label>`），其余角色暂留 `<TBD>`。
- 画像完成后，用结果补全 `<age>`、`<gender>`、`<game_id>` 等角色。
- 后续 step2_1 ~ step2_5 使用最终版 `schema_resolution.json`。

---

## step2_0：源数据深入理解

三阶段流程，最终产出 `schema_resolution.json` + `step2_0_source_data_analyze.md`。`step2_0_table_profile` 和 `step2_0_column_profile` 为内部中间态，不持久化。

### 阶段 1：构建 schema_resolution.json（骨架）

仅用 `step1_output_meta.json` 静态推断。必须完成**候选键验证**（空值率、唯一性、重复倍数），结果写入 `key_validation`。

### 阶段 2：执行画像 SQL

按 pipeline 顺序执行三条 SQL。`missing_rate` **必须存储为百分比值（0-100），计算式 `(count()-count(col))/count()*100`**。`step2_0_validation.sql` 的 label 结果写回 `schema_resolution.json`。

> ⛔ **表分类硬规则**：`step2_0_table_profile` 的每表 SELECT 必须包含 `classification` 列，用 CASE WHEN 硬计算：
> - `unique_user_id IS NULL` → `'维度表'`
> - `unique_user_id = total_rows` → `'1:1用户表'`
> - 否则 → `'1:N行为表'`
> 禁止 LLM 手工判断。反面案例 013113：`list_detail_info`（30000=30000）被误判为 1:N。
>
> ⛔ **提交前验证**：展开 SQL 后、submit 前，`grep -cF "AS classification" <expanded_sql>` 必须 ≥ 源表数（每表一行 classification）。
> 返回 0 → agent 漏掉了 CASE WHEN 展开。反面案例 013113：agent 展开了 table_profile SQL 但无 `AS classification`。
> 格式门禁步骤 2 Check 8 会再次验证已提交的 SQL 文件（双层兜底）。

### 阶段 2.5：格式门禁

> ⛔ **独立 todo item，不可合并到阶段 3。**
>
> **首先 `rm -f gate_data_verified.txt`**（覆盖上次运行的旧标记）。然后执行 `read_file("scripts/step2_0_format_gate.md")`，按文件执行 7 条 grep + 数据交叉验证。
> 不通过 → `delete` 文件重写 → 重新 grep。

### 阶段 3：补全 schema_resolution.json + 撰写分析文档

> ./ 硬前置：`test -f gate_data_verified.txt` 必须返回 true。文件不存在 → 数据交叉验证被跳过，**强制回退到阶段 2.5**。
> 已知故障 1012：agent 跳过全部数据交叉验证，导致 delimiter/null_rate/n_unique 幻觉。
> 此文件只由 `step2_0_format_gate.md` 成功完成写入——双重绑定，不可绕过。


从 `step2_0_column_profile` 查询全量字段数据补全 `<TBD>` 角色。撰写 `step2_0_source_data_analyze.md`：逐表 7 列表格 + 列表字段识别 + `## 列表字段检测结果` 汇总表。

> ⛔ **防幻觉规则**：所有字段的 n_unique、missing_rate、sample_values **必须从 ClickHouse 查询获取**，禁止 LLM 编造。
> **特征含义**优先从 `step1_output_meta.json` 的 `description` 取值，禁止机械复读字段名。

> 格式门禁是阶段 3 的硬前置——完成阶段 2 后必须先通过 `scripts/step2_0_format_gate.md`。

---

## step2_1：1:1 初步合表

执行 `scripts/step2_1_simple_merge.sql` → `scripts/step2_1_validation.sql`。以 `<user_table>` 为基表 LEFT JOIN 所有 1:1 表。超过一个 JOIN 时写成嵌套子查询（每层 1 个 JOIN），避免 ClickHouse 23.8 `Missing columns: '<user_id>'`。

**执行流程：**
1. 读取 `schema_resolution.json` 的 `key_validation`，检查 `max_duplication_factor`
2. `max_duplication_factor > 1` → 去重；`== 1` → 直接 JOIN
3. 验证行数和用户数未膨胀

---

## step2_2：原始特征清洗

> ⛔ **不可跳过。** 已知复发故障 0728-2：agent 从 step2_1 直接跳到 step2_3。
>
> **独立门禁文件**：`read_file("scripts/step2_2_format_gate.md")` 并按文件执行。独立 todo item，不可合并。

执行顺序：`scripts/step2_2_cleaning_report.sql` → `scripts/step2_2_feature_cleaning.sql` → `scripts/step2_2_validation.sql`。

**清洗决策基于 `step2_0_column_profile` 的预计算结果**（不重复聚合 step2_1）。核心边界：

| 条件 | 动作 |
|------|------|
| `<user_id>`、`<label>`、`<age>`、`<gender>` | 保护 |
| `sample_values` 含 `#` 或 `^`（列表字段） | 保护（空列表是有效信号） |
| `missing_rate > 50`（非列表字段） | 删除 |
| `n_unique <= 1` | 删除 |

> step2_2 门禁包含 CASE WHEN 正确性校验（`WHERE recommendation='KEEP' AND null_rate > 50` 须返回 0 行）和 EXCEPT 展开流程。详见 `scripts/step2_2_format_gate.md`。

---

## step2_3：复杂聚合与特征衍生

执行 `scripts/step2_3_feature_aggregation.sql` → `scripts/step2_3_validation.sql`。

> ⛔ **必须在 todo list 中拆为 9 个独立 item（含 0 号前置检查）**：

| 序号 | todo item | 内容 |
|------|-----------|------|
| 0 | **⛔ 前置检查** | `SELECT count() FROM {output_db}.step2_2_cleaning_report`——返回 0 → 回退执行 step2_2 |
| 1 | **列表字段识别** | 读取 md 的 `## 列表字段检测结果`，确认全量待分割字段（含 1:N 表如 `list_detail_info`） |
| 2 | **1:N 聚合** | 写 CTE 聚合 SQL。**检查该表的列表字段，在 CTE 内完成 splitByChar 二元展开**（见下方规则） |
| 3 | **列表字段分割** | 对每个字段执行 splitByChar + has() 二元展开 + 删除原字段 |
| 4 | **⛔ 提交前硬门禁** | `read_file("scripts/step2_3_format_gate.md")` 执行全部 grep。不通过 → 回到 todo 3 |
| 4b | **⛔ String 转换** | `read_file("scripts/step2_3_string_cast.md")`：城市列必须分级为 `{col}_tier`，**宽表禁止 `city` 原列**（过拟合）；其余残留 String **先采样**再 CAST（纯数字→Float64，逗号列表→length+is_empty，其他→LowCardinality(String)）。禁止未采样使用 `parseDateTimeBestEffort` |
| 5 | **SQL 建表 + 后端门禁** | submit 建表 → validation.sql：高基数门禁（不得残留 `String`/`Nullable(String)`，保护列除外）+ **城市原列门禁**（不得残留 `city` / `*城市*`，只留 `{col}_tier`） |
| 6 | **写文档** | `step2_3_feature_derivation.md` + `step2_3_high_cardinality_check.json` |
| 7 | **写部署特征契约** | 从生成 expanded SQL 时使用的同一份特征定义同步写 `step2_3_deployment_feature_contract.json`；不得解析 SQL/Markdown 反推 |

> **已知复发故障**：跳过列表分割、count-only（无 has()）、每个字段只产出 1 个二元特征、`list_detail_info` 的 1:N 表列表字段在聚合 CTE 中透传、**ClickHouse 23.8 扁平多 LEFT JOIN 报 Missing columns: 'usid'**、**残留高基数 String 未转换**。
> **防范**：todo 4 的 `scripts/step2_3_format_gate.md` 与 todo 4b 的 `scripts/step2_3_string_cast.md` 必须全部通过才能 submit。

### 关键规则速查

- **列名确认**：展开 CTE 前必须 `read_file` 读取 `step2_0_source_data_analyze.md`，禁止凭记忆写 SQL
- **GROUP BY**：ClickHouse 要求 `GROUP BY` 直接引用原生列名，**禁止 `GROUP BY toString(col)` 等函数包装**
- **超前清洗**：1:N 表聚合前从 `step2_2_cleaning_report` 查询 DROP 字段并排除
- **缺失值**：原始缺失保留 NULL，不以 0/均值/空字符串填充
- **字符串转换（submit 前）**：`#`/`^` 列表走 splitByChar。列名像城市（`city` / `*城市*`）必须按 `references/city_tier_map.json` 生成 `{col}_tier`（一线/新一线/二线/三线/三线及以下），**宽表禁止 `city` 原列（过拟合）**，用 `scripts/step2_3_city_tier_sql.py` 出 SQL。`北京市` 与 `北京` 同级；**县名不升市**。禁止只做 LowCardinality 保地名。其余 String 按 `scripts/step2_3_string_cast.md` **先采样 200 行再 CAST**：纯数字 → `toFloat64OrNull`；逗号列表 → `*_list_length` + `*_is_empty`；其他 → `LowCardinality(String)`。禁止未采样提交 `parseDateTimeBestEffort`。
- **字符串高基数门禁**：`step2_3_validation.sql` 检查 `system.columns`，残留 `String` / `Nullable(String)`（保护列除外）→ 阻塞。`LowCardinality(String)` 为合法落点（版本等），**不能**用来保留 `city` 原列。**禁止自我评估绕过门禁**
- **城市原列门禁（过拟合）**：`step2_3_wide_complete` 与 `step2_4_wide_userfiltered.csv` 不得出现 `city` / `city_name` / `*城市*` 原列，只保留 `{col}_tier`。validation.sql 第三条查询命中 → 失败；CSV 表头由 `step2_3_validate_deployment_contract.py` 再拦一次。
- **ClickHouse 23.8 多 JOIN**：同一 FROM 中不得有两个及以上 LEFT/INNER/CROSS JOIN。必须嵌套子查询，每层恰好 1 个 JOIN，ON 用表别名限定 `<user_id>`。禁止 `USING`。若报 `Missing columns: 'usid'`（或其它用户键），**不要**创建 `step2_3_test*` / 物化诊断表，立刻按 `scripts/step2_3_format_gate.md` 附录改写成嵌套 JOIN 后重提这一条 CREATE。

> 详细执行规则（列表分割、嵌套 JOIN、残留 String 采样 CAST）见 `scripts/step2_3_format_gate.md` 与 `scripts/step2_3_string_cast.md`。
> 1:N 表列表字段的拆分模板见该文件附录。
> 部署契约格式、关系 plan 和字段级校验规则见 `references/deployment_feature_contract.md`。这是新增
> 交付契约，不替代或改变 step2_3 原有 SQL、特征衍生和门禁逻辑。

---

## step2_4：用户过滤

执行 `scripts/step2_4_user_cleaning.sql` → `scripts/step2_4_validation.sql`。

- 依据 step2_0 记录的合法值域过滤年龄/性别不合法用户
- 门禁验证：`<user_id>` 唯一、`<label>` 0/1、列对账（step2_4 vs step2_3 `system.columns` 列数一致）
- 写入 `step2_4_user_filter_report.json`
- **门禁通过后** `read_file("scripts/step2_4_export_csv.md")`，按上文「CSV 导出方式优先级」导出
  `step2_4_wide_userfiltered.csv`。独立 todo，不可跳过。
- CSV 导出后运行 `scripts/step2_3_validate_deployment_contract.py`。检查器会对账宽表列、源表
  Schema、alias/字段、关系 plan 与 expanded SQL 哈希，但不使用聚合函数白名单判断开放式
  SQL 语义。通过时发布新契约；失败时尝试修正新增契约，仍未通过则不登记该契约，但不得
  阻止原有 Step2 产物和 receipt 定稿。

---

## step2_5：定稿 receipt

按 `scripts/step2_5_finalize.md` 验收并写 `receipt.json`。不写 SQL、不连 CH。

---

## 完成自检清单

提交 receipt 前逐项确认：

- [ ] `schema_resolution.json` 已分阶段补全，`source_tables` 与 `output_meta.projection_tables` 一致
- [ ] step2_0 key_validation 中候选键验证和 label 验证已通过
- [ ] **`step2_0_source_data_analyze.md`** 已写入 workspace
- [ ] `step2_1_wide_simple` 已创建，连接验证通过
- [ ] `step2_2_wide_cleaned` 已创建：
  - [ ] `step2_2_cleaning_report` 数据来源为 `step2_0_column_profile`（不重复计算）
  - [ ] 清洗决策正确性门禁通过（`WHERE recommendation='KEEP' AND null_rate > 50` 返回 0 行）
  - [ ] `/*__CLEANING_EXCEPT_CLAUSE__*/` 已从 cleaning_report 的 DROP 字段查询展开，非手工列出
  - [ ] `step2_2_cleaning_report.json` 已写入
- [ ] `step2_3_wide_complete` 已创建：
  - [ ] **提交前硬门禁通过**：grep splitByChar 命中数 ≥ 待分割字段数
  - [ ] 高基数门禁 `system.columns` 查询返回 0 行（无残留 `String`/`Nullable(String)`，保护列除外）
  - [ ] 残留 String 已按采样结果转换（city_tier / numeric / comma_list / LowCardinality），有 `string_cast_plan.txt`
  - [ ] 若存在城市列：宽表与 CSV **只有** `{col}_tier`，**没有** `city` 原列（过拟合）
  - [ ] 已写入 `progress_step2_3.txt`
  - [ ] `## 列表字段检测结果` 中**全量 delimiter 字段**（含 1:N 表如 `list_detail_info`）已执行 splitByChar
  - [ ] 原列表字段已从 step2_3_wide_complete 中删除（含 `ld_*` 前缀的 1:N 表原始列表字段）
  - [ ] `step2_3_feature_derivation.md` 已写入
  - [ ] `step2_3_high_cardinality_check.json` 已写入（status 以门禁 SQL 结果为依据，非自我评估）
  - [ ] `step2_3_deployment_feature_contract.json` 与 expanded SQL 由同一份特征定义生成
  - [ ] JOIN 为嵌套 1-JOIN-per-layer（`step2_3_check_join_nesting.py` 通过），无 `step2_3_test*` 诊断表
- [ ] `step2_4_wide_userfiltered` 已创建，`<label>` 为 0/1，`step2_4_user_filter_report.json` 已写入
- [ ] **`step2_4_wide_userfiltered.csv`** 已按 `scripts/step2_4_export_csv.md` 导出（MCP 优先，未做凭据搜索）
- [ ] 若发布部署特征契约，其 `validation.structural_validation.passed=true`
- [ ] 无 `_tmp_*`、`_ft_*`、`fe_` 等非标准前缀残留
