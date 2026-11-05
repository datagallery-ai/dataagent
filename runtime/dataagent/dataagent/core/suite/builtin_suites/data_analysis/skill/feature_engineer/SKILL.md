---
name: feature-engineer
description: >-
  执行 step2_0→step2_5 标准特征工程流水线，产出训练宽表。
  Use when 采样完成后。
disable-model-invocation: true
---

# Feature Engineering Pipeline（step2_0 → step2_5）

输入：采样阶段传来的 `step1_output_meta.json` + `step1_sample_stats.json` + 全部交付表（output_database 内与源表同名）。
输出：`step2_4_wide_userfiltered.csv`（模型唯一训练数据）、`step2_2_deployment_feature_contract.json`
（供 NL2SQL 在全量源表上确定性回放特征）以及各阶段分析产物。

**路径**：`read_file` 与 bash 一律使用 `skill/feature-engineer/scripts/...` 与 `skill/feature-engineer/references/...`。禁止裸写 `scripts/...` 或 `references/...`，禁止写成 `skill/feature_engineer/...`。

具体表名、列名、地理列模式只写在 `skill/feature-engineer/references/scene.yaml`。`skill/feature-engineer/scripts/*.sql` 与全部命令只用占位符（`<user_table>`、`<user_id>`、`<list_col>` 等）。地理列门禁通过 `skill/feature-engineer/scripts/step2_2_city_tier_sql.py` 读取该配置并展开；禁止直接读 `scene.yaml`，禁止把场景表名/列名写进 SQL 模板或其它脚本。

`<catalog_columns>` / `<catalog_tables>` 由 step2_0/2_1/2_3 的驱动脚本按 `skill/feature-engineer/references/data_source.json` 自动绑定，**不要手工替换**；其余占位符按工作区画像结果与 step1 交接产物绑定。换仓规则见 `skill/feature-engineer/references/data_source.md`。

## 数据操作规则

| 步骤 | `<warehouse>` 访问方式 | 本地文件 |
|------|-------------------|---------|
| **step2_0 ~ step2_3** | 仅 `submit_resource_job` / `poll_job` / `cancel_job` / `collect_job`<br>**严禁 Bash 连接 `<warehouse>`**<br>**严禁导出中间 CSV**（所有表数据必须留在 `<warehouse>`）<br>长 SQL 用 `command_file` 提交，禁止直接传 `command` | 仅 `.md`、`.json` 分析产物 |
| **step2_4** | 建表和校验只用 MCP。CSV 导出按「CSV 导出方式优先级」执行 | `.json` + **`step2_4_wide_userfiltered.csv`** |

**表结构与字段含义**：通过 step2_0 的数据画像直接从 `<warehouse>` 分析源数据推断，列角色初始信息来自采样交接的 `step1_output_meta.json`（列/类型/join）和 `step1_sample_stats.json`（`projection_tables[].type`）。

### CSV 导出方式优先级（step2_4 门禁通过后）

**顺序不可颠倒**：① MCP `submit_resource_job` 分批 `SELECT` → `poll_job` → `collect_job` → `step2_4_driver.py append-csv`（唯一默认方式；不需要密码，**不要** `curl` 直连 HTTP）；② 仅当进程环境已注入非空 `WAREHOUSE_PASSWORD` 时允许一次只读直连；③ 直连首次认证失败（401/403/`Authentication failed`）**立即**回到 ①。

禁止搜索凭据（配置文件、进程命令行、DataAgent YAML、datasource 表、猜密码）。分批 SQL 与 OFFSET 由 `export-plan` 生成；导出优先级、直连门禁与禁忌清单见 `skill/feature-engineer/scripts/step2_4_export_csv.md`（独立 todo，必须 `read_file`）。

### collect→落盘统一规则

每次 `collect_job` 成功后，用共享 CLI 把结果落成清单里的 `collect_as`（成功后写边车 `<out>.via-save-collect`）。给文件路径就能落盘，不必先做来源合法性审查。

```bash
# 大结果（collect 响应含 Full tool result file:）：直接指 tool-results 或 --latest
python skill/feature-engineer/scripts/step2_driver_common.py save-collect \
  --source tool-results/<id>.txt --out <collect_as> [--extract-rows]
# 或：
python skill/feature-engineer/scripts/step2_driver_common.py save-collect \
  --latest --out <collect_as> [--extract-rows]

# ≤32KB 小结果：把 collect 响应写临时文件再 --source（heredoc 写临时文件仅限 ≤32KB）
python3 <<'PYEOF'
from pathlib import Path
Path("tmp_collect.json").write_text(<collect_response_json>, encoding="utf-8")
PYEOF
python skill/feature-engineer/scripts/step2_driver_common.py save-collect \
  --source tmp_collect.json --out <collect_as> [--extract-rows]

# 批量落盘：--source-glob 按 mtime 排序，并排除已被 .via-save-collect 消费的文件
# 词项临时文件 / CSV 导出的 tool-results 都可用
python skill/feature-engineer/scripts/step2_driver_common.py save-collect \
  --batch --source-glob 'tool-results/*.txt' --name-prefix <prefix> \
  --start-index 16 [--out .]
```

操作要点：

- **立即落盘**：收完一批 / 一次 collect 后马上按对应 `collect_as` 落盘，不要攒一批再写。
- **落盘即完成，提交前先核对**：提交前 `ls` / `check-collect` 对照 manifest；已有非空 `collect_as`（带 `.via-save-collect`）的 job 不要再 submit。
- **防重复提交 / 守卫一次通过**：守卫报缺失或 0 行时，先补对应的 `save-collect` 再重跑守卫，不要重新 submit / poll / collect 同一 job，也不要反复整批重跑。
- **rows=0 非失败**：`save-collect` 打印 `rows=0` 时不是失败（空表合法）；仅当 `--expect-rows` 要求非空时再按打印补救。
- **check-collect**：各阶段 `after_collect` / `gate` 已自动前置；缺文件、无标记或空结果 → 按打印的补救命令重落盘。
- **`--extract-rows`**：词项频次、清洗画像等「普通行列表」落盘时加此开关（只写 rows）。

## 进度标记

中断后先 `cat` 恢复，禁止凭对话记忆回忆「做到哪了」。

| 文件 | 何时写入 | 写入方 | 说明 |
|------|----------|--------|------|
| `progress_step2_1.txt` | `step2_1_wide_joined` 校验通过 | `step2_1_driver.py gate` | step2_1 的门禁标记 |

其余阶段的**恢复信号就是各自的门禁标记**：`gate_data_verified.txt`（step2_0）、`gate_data_verified_2_2.txt` + `step2_2_feature_gate.json`（step2_2）、`gate_data_verified_2_3.txt` + `step2_3_cleaning_report.json`（step2_3）、`gate_data_verified_2_4.txt` + `step2_4_user_filter_report.json`（step2_4）。这些标记只能由对应 `gate` 写出，**禁止手工 `echo`/手写绕过**。

恢复时先 `cat progress_step2_1.txt gate_data_verified*.txt 2>/dev/null`；已完成的阶段不要重做，从第一个缺标记的阶段继续。


---

## Pipeline 总览

`⛔` = 独立门禁 todo，不可跳过。

| 步骤 | `<warehouse>` 表 | Workspace 产物 | 执行顺序 |
|------|---------------|----------------|----------|
| step2_0_0 | — | `schema_resolution.json`（骨架） | `step2_0_driver.py prepare` |
| step2_0_1 | `step2_0_describe_columns`、`step2_0_table_profile`、`step2_0_column_profile` | `raw_describe_columns.json`、`step2_0_profile_dump.json` | `prepare` → submit describe → `purge`（幽灵列 + 展开画像）→ submit 两个 `*_expanded.sql` → `render` |
| step2_0_2 | — | `step2_0_profile_report.md`、`list_fields.txt`、`step2_0_semantic_todo.json` | `step2_0_driver.py render` → [`set-constant`] |
| **step2_0_3 ⛔** | — | **`gate_data_verified.txt`** | `step2_0_driver.py gate` |
| step2_1 | `step2_1_wide_joined` | `progress_step2_1.txt` | `step2_1_driver.py prepare` → submit `join_expanded.sql` → submit `validation_expanded.sql` → `step2_1_driver.py gate` |
| step2_2 | `step2_2_wide_aggregated` | `step2_2_feature_plan.json`、`step2_2_feature_derivation.md`、`step2_2_deployment_feature_contract.json`、`step2_2_feature_gate.json`、`gate_data_verified_2_2.txt` | `step2_2_driver.py prepare` → 按 `batch_groups` 分批词项 → `after_collect` 到 `finalize`/`gate`（validation 拆条仅供审阅，只提交 combined SELECT → `raw_wide_stats.json`） |
| **step2_3 ⛔** | `step2_3_cleaning_report`、`step2_3_wide_cleaned` | **`gate_data_verified_2_3.txt`、`step2_3_cleaning_report.json`** | `prepare`（超限自动分批）→ ①建表 jobs → ②combine（须全部 batch 建表完成后）→ ③combined SELECT → ④save-collect --extract-rows → ⑤check-collect → finalize → gate |
| step2_4 | `step2_4_wide_userfiltered` | `gate_data_verified_2_4.txt`、`step2_4_user_filter_report.json`、**`step2_4_wide_userfiltered.csv`** | `prepare` → 建表 / stats / columns / count → `gate` → `export-plan --batch-size 999` → `save-collect --batch` → `append-csv` → `step2_5_driver.py validate-contract` |
| step2_5 | — | **`receipt.json`** | **`step2_5_driver.py finalize`**（validate-contract → receipt → verify-all） |

每文件一条 SQL，建表 `CREATE OR REPLACE TABLE ... AS SELECT`，校验 `SELECT`。模板里的 `{{output_database}}` 与 `<...>` 占位符由展开脚本或工作区产物绑定，提交前不得残留未绑定占位符。后续 step2_1 ~ step2_5 使用最终版 `schema_resolution.json`。

---

## step2_0：源数据深入理解

子阶段 `step2_0_0` → `step2_0_1` → `step2_0_2` → `step2_0_3`，最终产出 `schema_resolution.json` + `step2_0_profile_report.md`。`step2_0_describe_columns` / `step2_0_table_profile` / `step2_0_column_profile` 是中间表，不写进 receipt。

> ⛔ **step2_0 由 `step2_0_driver.py` 驱动，禁止手写 SQL 动态块或 md 表格。** 流程固定为 **prepare → describe → purge → render → gate**，中间只插入 MCP 提交与 `save-collect` 落盘。**不要**把 `step2_0_1` 拆成 1a/1b 两套入口——describe job 与画像展开同属 `step2_0_1`，由 prepare → purge 串起。

> ⛔ **画像一次成型 + 输出收敛（违反即本阶段失败）**：
> 1. **一次成型**：`step2_0_profile_report.md` 由 `render` **一次性生成**；禁止对已落盘的 `raw_table_profile` / `raw_column_profile` 反复 re-render 或多轮重写描述。画像产物落盘即完成，禁止重复 submit / collect 同一画像 job。
> 2. **输出收敛**：每张表的画像描述只写结论性内容（分布形态 / 缺失率 / 取值域），长度有上限；禁止逐列长篇展开，禁止把画像内容重复贴进多条消息。

```bash
# 1. prepare：构建骨架 + 生成 system.columns 描述查询（job=step2_0_describe_columns，
#    collect_as=raw_describe_columns.json）
python skill/feature-engineer/scripts/step2_0_driver.py prepare \
  --meta step1_output_meta.json --stats step1_sample_stats.json --schema schema_resolution.json

# 2. 按清单 submit describe → poll → collect → save-collect 落盘为 raw_describe_columns.json

# 3. purge：幽灵列校验（就地修正 meta + 备份 step1_output_meta.json.orig.json +
#    ghost_columns 审计列表）+ 展开两条画像 SQL
python skill/feature-engineer/scripts/step2_0_driver.py purge \
  --describe raw_describe_columns.json --meta step1_output_meta.json --schema schema_resolution.json

# 4. 按清单 submit 画像 SQL → poll → collect → save-collect
#    （raw_table_profile.json / raw_column_profile.json）
#    ⚠ 先描述后画像：顺序不可颠倒（purge 依赖 describe 结果剔除幽灵列）

# 5. render：汇总画像 → 补全 schema → 渲染 md
python skill/feature-engineer/scripts/step2_0_driver.py render \
  --raw raw_table_profile.json --raw raw_column_profile.json \
  --meta step1_output_meta.json --schema schema_resolution.json

# 6. 逐个 set-constant（按 --table + --field，禁止按行号）
python skill/feature-engineer/scripts/step2_0_driver.py set-constant \
  --md step2_0_profile_report.md --table <table> --field <field> --value semantic|false

# 7. gate：重渲染 diff + 语义常量校验
python skill/feature-engineer/scripts/step2_0_driver.py gate \
  --md step2_0_profile_report.md --dump step2_0_profile_dump.json \
  --meta step1_output_meta.json --schema schema_resolution.json
```

### step2_0_0 / step2_0_1：骨架、描述与画像 SQL

`prepare` 产出 `schema_resolution.json` 骨架（`source_tables` 含 `table_type` / `user_key`、`user_table`、`user_id`、`label`、`age`、`gender`、`user_id_columns`）与描述查询 `step2_0_1_describe_columns_expanded.sql`（建 `step2_0_describe_columns`）。

`purge` 消费 `raw_describe_columns.json`：

- **幽灵列机制**：只在确实存在幽灵列时备份 `step1_output_meta.json.orig.json` 并就地修正 meta；无幽灵列则原样通过（不备份）。`schema_resolution.json` 的 `ghost_columns` 为审计记录列表 `[{table, column}, …]`，按 `(table, column)` 去重追加。
- 再展开两条画像 SQL，**每个文件恰好一条**：

| expanded 文件 | 用途 |
|---------------|------|
| `step2_0_1_describe_columns_expanded.sql` | 建 `step2_0_describe_columns`（`prepare` 产出） |
| `step2_0_1_table_profile_expanded.sql` | 建 `step2_0_table_profile`（已含 `AS classification`） |
| `step2_0_1_column_profile_expanded.sql` | 建 `step2_0_column_profile`（`missing_rate` 已按百分比 0-100 计算） |

候选键验证（空值率、唯一性、重复倍数）由 `render` 补算。

> ⛔ **表分类硬规则**：`classification` 按固定 CASE WHEN 规则生成（每表一行）：
> - `total_rows = 0` → `'维度表'`（**0 行空表自动降级**，`max_duplication_factor=1.0`）
> - `unique_user_id IS NULL` → `'维度表'`
> - `unique_user_id = total_rows` → `'1:1用户表'`
> - 否则 → `'1:N行为表'`
> 分类由上述规则唯一确定，禁止另行判断。

### step2_0_2：补全 schema + 渲染分析文档（由 `render` 完成）

`render` 把画像 payload 汇总成 `step2_0_profile_dump.json`，补全 `key_validation` 并回填 `user_key`，渲染 `step2_0_profile_report.md`，并写出 `list_fields.txt` 与 `step2_0_semantic_todo.json`。

渲染后，逐个处理 `step2_0_semantic_todo.json` 中列出的 `constant:TODO` 行：判断该字段的多个取值是否语义上等价于"无信息"（如 `未知`/`none`/`-`/`其他`）。
- 是语义常量 → `set-constant --value semantic`
- 否（正常多值字段）→ `set-constant --value false`

必须用 `step2_0_driver.py set-constant --table <table> --field <field> --value ...` 定位；**禁止按行号**、禁止改动其他列。**语义判定一次完成**：每个字段只判定并 `set-constant` 一次，禁止对同一字段反复改值重跑 `gate`。判定完成后进入 `gate`。

> md 中所有 n_unique、missing_rate、sample_values 照实引用，不要改动。

### step2_0_3：格式门禁（由 `gate` 完成）

> ⛔ **独立 todo item，不可合并到 `render`。** 前置：`render` 已渲染 md 并完成语义判定。

`gate` 一次性执行全部门禁检查：把 `step2_0_profile_report.md` 与 `list_fields.txt` 用同一份 dump/meta/schema **重新渲染后逐行 diff**（只允许分布分析列的 `constant:` 取值差异，其余任何改动都报错），再校验语义常量编辑（禁止残留 `constant:TODO`；`constant:semantic` 只允许出现在候选字段上；`constant:true/false` 必须与 `n_unique` 一致）。

- 通过 → 写 `gate_data_verified.txt`，`exit 0`
- 不通过 → 逐条打印违规字段与期望值，`exit` 非 0。修正后重跑。

> ⛔ `gate_data_verified.txt` 是 step2_0 完成的唯一标志，禁止手工 `echo` 该文件。

---

## step2_1：1:1 初步合表

> ⛔ **step2_1 全流程脚本化**：合表 JOIN、列前缀、门禁判定全部由固定脚本生成，禁止手写 SQL 与手工判定。

```bash
# 1. 驱动脚本：展开合表 SQL + validation 门禁 SELECT（并自动绑定 <catalog_columns>），
#    写出 step2_1_run.json 提交清单
python skill/feature-engineer/scripts/step2_1_driver.py prepare \
  --schema schema_resolution.json --meta step1_output_meta.json --out-dir .
# 2. 按 step2_1_run.json 的 jobs 顺序逐条 submit_resource_job（command_file 指向清单里的文件）
#    → poll_job → collect_job → save-collect 落盘（validation 存为 raw_step2_1_validation.json）
# 3. 跑清单里的 gate 命令（已自动前置 check-collect --expect-rows）
python skill/feature-engineer/scripts/step2_1_driver.py gate \
  --schema schema_resolution.json --raw-validation raw_step2_1_validation.json
```

`step2_1_driver.py prepare` 生成合表 SQL；若某张 `1:1用户表` 的 `max_duplication_factor ≠ 1`，脚本阻塞退出（禁止静默去重）→ 回到 step2_0 修正分类，禁止手写 JOIN。`select_right_tables` 对 `total_rows=0` 的表**兜底跳过**（不参与 JOIN），避免空表把结果集归零。

产物：`step2_1_join_expanded.sql`（建 `step2_1_wide_joined`）、`step2_1_validation_expanded.sql`（门禁 SELECT）、`step2_1_merge_plan.json`（透传产物，无需读取）、`step2_1_run.json`（提交清单）。

---

## step2_2：复杂聚合与特征衍生

产出聚合宽表 `step2_2_wide_aggregated`，不做清洗。

> ⛔ **step2_2 全流程脚本化**：1:N 聚合函数、列表二元展开、列名后缀、丢弃判定、EXCEPT/CAST、部署契约全部由固定脚本按 `skill/feature-engineer/references/aggregation_policy.json` 生成。**禁止手写 CTE、禁止自选聚合函数、禁止手写契约。**

> **🎯 特征加工下沉边界**：只有 **1:N 表**的最终特征在聚合 CTE 内部一次生成（非列表列聚合 + 列表二元展开/长度/空标记 + `_row_count`），CTE 之后只做拼接。以下三类**不进 CTE**，留在最外层（或维度 CROSS JOIN 层）：
> - **1:1 表列表字段**：1:1 表已由 step2_1 拍平进 `step2_1_wide_joined` → 最外层直接 `has(splitByChar(...))`，**无 `max()`**
> - **地理分级 `{col}_tier`**：基表列在最外层生成；维度表列在各自 CROSS JOIN 层生成（逐层 scrub 原列）
> - **残留 String 采样 CAST**：读 `string_cast_plan.txt`，落最外层
>
> 下游形态固定为**嵌套 1-JOIN-per-layer 链**（`w.* EXCEPT(...)` → `j1` → `j2` → … → 最外层），原始列逐层透传、最终特征从 CTE 拼回；**禁止扁平多 JOIN**。

> ⛔ **收词项硬约束（违反即本阶段失败，必须纠正后重跑）**：
> 1. **幂等防重 / 落盘即完成，提交前先核对**：提交前先 `ls raw_terms_*.json`，或跑 `check-collect --manifest step2_2_run.json --expect-rows --out-dir .`；对照 manifest 的 `term_jobs_pending` / `batch_groups`。**仅缺失**的 `raw_terms_<i>.json` 才 `submit` 对应 `step2_2_term_query_<i>.sql`。同一 job **只提交一次**；禁止对已 poll 完成并 collect 过的 job 重复 submit。缺文件 / 守卫报缺失时**只补 `save-collect`**，禁止重新 submit / poll / collect 同一 job（同顶部「防重复提交」「守卫一次通过」）。
> 2. **大/小结果怎么落**：大结果（响应含 `Full tool result file:`）用 `save-collect --source tool-results/<id>.txt` 或 `--latest`；≤32KB 小结果把 collect 响应原样写临时文件（如 `tmp_terms_<i>.json`）再 `save-collect --source … --out raw_terms_<i>.json --extract-rows`。允许把多个小查询写进同一 expanded SQL / 一次 submit。
> 3. **分批提交 + 立即落盘**：每轮按 `batch_groups` 提交（`batch_size` 默认 `min(8, n)`），禁止一次提交全部词项 job，也禁止一次只提交一两个就长时间空转。批内每个 job：`submit → poll → collect` 后**立即**按该 job 的 `collect_as` 落盘（`--source` / `--latest`，或对已落盘临时文件 / `tool-results` 用 `save-collect --batch`）；该批全部落盘后再开下一批。
> 4. **完整性放行 + 失败切换出口**：全部词项非空落盘后，`after_collect` 前置的 `check-collect --expect-rows` 才放行 `sample`。带着缺失清单继续 = 失败；守卫失败时只补对应 `save-collect`。同一词项落盘失败累计重交 >2 次必须换方法（读 `tool-results` 直落 / 临时 json + `--source`），禁止无限循环。

```bash
# 入口：展开词项频次 SQL，写出 step2_2_run.json（含 batch_groups / term_jobs_pending）
python skill/feature-engineer/scripts/step2_2_driver.py prepare \
  --schema schema_resolution.json --meta step1_output_meta.json \
  --dump step2_0_profile_dump.json --list-fields list_fields.txt --out-dir .
# 随后严格按上方「收词项硬约束」按 batch_groups 分批：
# 批内逐个 submit → poll → collect → 立即 save-collect 落盘；
# 全部落盘后再照 after_collect 进入 sample / finalize / gate。
```

`after_collect` 链固定为 `prepare → sample →（有候选则 string-plan）→ finalize → gate`；`sample` 无候选时直接跳 `finalize`，`string-plan` 由 `--string-plan` 回填 `finalize`。命令形参全部写死在清单里，**禁止手工拼命令或改写 expanded SQL**，**禁止**产生 `*_v2` / `*_fixed` / `agg_*` 等 SQL 变体文件。`gate` 写出 `gate_data_verified_2_2.txt` 并清除 `after_collect` 后本阶段结束。

> **必须在 todo list 中拆为 4 个独立 item**：

| 序号 | todo item | 内容 |
|------|-----------|------|
| 0 | **⛔ 前置检查** | 确认 `step2_1_wide_joined` 已创建（`step2_1_driver.py gate` 已通过） |
| 1 | **prepare + 收词项** | 跑 `prepare`，然后遵守上方「收词项硬约束」。按 `batch_groups` 分批；批内立即 `save-collect` 落盘；守卫一次通过，勿无限重交 |
| 2 | **按清单执行到 finalize** | 照 `after_collect` 依次跑 `sample`（→ 提交采样 → `string-plan`）→ `finalize`（产出 expanded SQL / **validation_1/2/3（仅审阅）** / **validation_combined** / 计划 / 契约 / derivation + `.sha256`；此时 `after_collect` 指向 `check-collect && gate`，先不要跑） |
| 3 | **建表 + 门禁** | 先提交建表（CREATE，无 collect）；再**只提交** `collect.command_file`（`step2_2_validation_combined.sql` 单条 combined SELECT）→ `save-collect` 落到 `raw_wide_stats.json`（countIf **始终单行**，`n_*` 命中数为 0 才通过）；然后跑清单里的 `gate`（**2a~2l**）；通过后 `after_collect` 消失 |

产物：`step2_2_term_query_*.sql` / `raw_terms_*.json`（0 基；**已落盘即完成，勿重提交对应 job**）、`step2_2_feature_aggregation_expanded.sql`（建表）+ `.sha256`、`step2_2_validation_1/2/3.sql`（审阅拆分）、`step2_2_validation_combined.sql`、`raw_wide_stats.json`、`step2_2_feature_plan.json`、`step2_2_deployment_feature_contract.json`、`step2_2_feature_derivation.md`、`step2_2_feature_gate.json`、`gate_data_verified_2_2.txt`、`step2_2_run.json`。

### 关键规则

- **聚合函数按 policy 查表，禁止自选**：类型 → 聚合函数与丢弃判定只写在 `skill/feature-engineer/references/aggregation_policy.json`（数值 `sum`+缺省填 0，日期/布尔/低基数字符串 `max`+保留 NULL；`term_query_batch_size` / `dimension_row_limit_threshold` 同此文件）。**不得自行选择聚合函数**，也不得在 SQL 模板或门禁里另写阈值。
- **列名禁止中文**：所有新列名只允许 `[A-Za-z0-9_]`；中文词项一律转拼音，如 `_term_rexue`。中文只作为 `has(splitByChar(...), '中文')` 的字符串参数。
- **GROUP BY**：`<warehouse>` 要求 `GROUP BY` 直接引用原生列名，**禁止 `GROUP BY toString(col)` 等函数包装**。
- **缺失值（1:N 聚合层）**：数值聚合 `sum` 无行时落 0（`fill`）；日期/布尔/字符串 `max` 保留 NULL。非聚合透传列仍保留原始 NULL，**不得用 0/均值/空字符串填充**。
- **ID 守卫**：`n_unique / total_rows ≥ 0.9` 的列（如行级唯一时间戳、UUID）判为标识符并丢弃（reason `identifier_like`），不得产出 `sum(order_id)` 这类特征；丢弃列及 reason 记录在 `step2_2_feature_plan.json`。
- **用户键去重**：每个聚合 JOIN 层投影必须 `{alias}.* EXCEPT (<user_id>)`，只保留基表主键；维度层若含 `<user_id>` 同理 EXCEPT。门禁 **2l** 强制检查。
- **EXCEPT 别名 Bug**：基表侧需 EXCEPT 的列转移到最外层剔除（避免外层 `has()`/`CAST` 引用已被内层剥掉的列 → Code 47）；由 `finalize` 自动处理，禁止手改。
- **门禁失败恢复（sha256）**：`finalize` 确定性重生成 `step2_2_feature_aggregation_expanded.sql` 与同名 `.sha256`（GNU `sha256sum -c` 可直接校验）。不一致时**丢弃手改文件**、以重生成文件为准（可 `diff` 对照）；禁止为过门禁手改 expanded SQL。
- **维度表 CROSS JOIN**：`total_rows=0` 跳过；行数超过 `dimension_row_limit_threshold` 时收敛为 `CROSS JOIN (SELECT * FROM ... LIMIT 1)`。
- **列清单缺失**：`finalize` 直接报障，禁止凭记忆补 SQL。
- **门禁不可自我评估**：`<catalog_columns>` 残留 `String`/`Nullable(String)`（保护列除外）或残留地理原列 → 阻塞；`LowCardinality(String)` 仅允许版本号等既有低基数列，**禁止**用于保留地名。门禁组为 **2a~2l**。

> **细节文档（仅在门禁报错或需确认规则时 `read_file`）**：
> - 列表字段分割（1:N 下沉 CTE / 1:1 最外层）、统一命名规约、嵌套 JOIN 写法与门禁报错定位 → `skill/feature-engineer/scripts/step2_2_format_gate.md`
> - 残留 String 的候选分类与 CAST 落点规则 → `skill/feature-engineer/scripts/step2_2_string_cast.md`
> - 部署契约结构、relation plan 与校验规则 → `skill/feature-engineer/references/deployment_feature_contract.md`

---

## step2_3：聚合宽表用户级清洗

> ⛔ **不可跳过。独立 todo item，不可合并。**

> ⛔ **step2_3 由 `step2_3_driver.py` 驱动**：禁止手工列举 EXCEPT 字段名、禁止手工判定清洗决策、禁止手工拼列清单查询、禁止手工执行两次展开与门禁。EXCEPT 动态块由 `finalize` 按实际列清单展开。

**清洗范围** = 聚合宽表 `step2_2_wide_aggregated` **一张表**（含 1:1、1:N 聚合与维度衍生列），画像粒度为**用户级**。判定阈值与保护列只写在 `skill/feature-engineer/references/cleaning_policy.json`，**禁止另写数值**；`null_rate > null_rate_drop_threshold_percent` 或 `n_unique <= constant_drop_max_unique` 的非保护列删除，命中 `protected_role_keys` 的列保护。列表字段不参与清洗判定（step2_2 已展开为二元特征）。

`gate` 校验（全部由脚本强制，不通过时逐条打印违规字段）：cleaning_report 正确性（KEEP 且 null_rate 超阈值即判定被绕过）、保护列必须 KEEP、常量列必须 DROP、`step2_3_wide_cleaned` 完整性（`n_rows == n_user_id` 且列覆盖一致）、DROP 残留为 0。通过后写 `gate_data_verified_2_3.txt` + `step2_3_cleaning_report.json`（均不可手工 `echo`）。

```bash
# 0. 查询 step2_2_wide_aggregated 实际列清单（<catalog_columns>，取 name/type），
#    collect → save-collect 存为 raw_step2_2_columns.json

# 1. 入口：展开清洗画像 SQL（超 report_sql_max_bytes 时自动按 report_batch_columns 拆
#    step2_3_cleaning_report_expanded_batch_<i>.sql + combine UNION ALL + 单条 combined SELECT），
#    写出 step2_3_run.json
python skill/feature-engineer/scripts/step2_3_driver.py prepare \
  --schema schema_resolution.json --raw-columns raw_step2_2_columns.json --out-dir .

# 2. 提交顺序不可颠倒（否则 Code 60）：
#    ① 逐条提交 jobs 建表 batch → poll 全部完成
#    ② 大表时单独提交 combine（必须在全部 batch 建表完成后）
#    ③ 提交 collect 单条 combined SELECT
#    ④ 立即 save-collect --extract-rows 落盘 raw_cleaning_report.json
#       （禁止把建表 0 行响应存成画像）
#    ⑤ check-collect → finalize → 建清洗表 / validation → gate
#    收集失败时用 rescue-collect 诊断（--rescue-main-table 生成宽松主表 SELECT）
```

`after_collect` 链固定为 `prepare → finalize → gate`；命令形参由脚本写死在清单里，**禁止手工拼命令或改写 expanded SQL**。`finalize` 对 raw report **按行全字段哈希去重**（检测整批重复写入并告警）；`--raw-report` 必填可重复。`gate` 写出 `gate_data_verified_2_3.txt` 并清除 `after_collect` 后本阶段结束。

> **落盘即完成，提交前先核对**：建表 / combine / combined SELECT 各 job 只提交一次；`check-collect` / `gate` 报缺时只补对应 `save-collect` 后重跑守卫（同顶部「守卫一次通过」），禁止整批重跑。
> `finalize` 若发现某 DROP 字段不是 `step2_2_wide_aggregated` 的列，会直接报障退出。
> `gate_data_verified_2_3.txt` 不可手工 `echo` 绕过；文件不存在即视为清洗门禁未做。
> 收集失败救援：`python skill/feature-engineer/scripts/step2_3_driver.py rescue-collect --out-dir .`（可选 `--rescue-main-table`）。

---

## step2_4：用户滤空

> ⛔ **step2_4 由 `step2_4_driver.py` 驱动**：禁止手写清洗/校验 SQL、禁止手工替换占位符、禁止手工把 validation 结果抄成 `step2_4_user_filter_report.json`。

```bash
# 1. 入口：绑定清洗建表与三条查询 SQL（占位符取自 schema_resolution.json 与 data_source.json），
#    写出 step2_4_run.json 提交清单
python skill/feature-engineer/scripts/step2_4_driver.py prepare \
  --schema schema_resolution.json --out-dir .

# 2. 按 step2_4_run.json 的 jobs 顺序逐条 submit_resource_job（command_file 指向清单文件）：
#    建表 → stats validation → columns validation → count
#    → poll_job → collect_job → save-collect 落盘；collect 后照 after_collect（已前置 check-collect）跑 gate

# 3. gate：判定用户键唯一 / label 0-1 / 列对账，写出过滤报告与标记，并清除 after_collect
python skill/feature-engineer/scripts/step2_4_driver.py gate \
  --stats raw_step2_4_stats.json --columns raw_step2_4_columns.json --out-dir .
```

- 依据 step2_0 记录的 `<age>` / `<gender>` **滤除空值与空串**（列名由 `prepare` 绑定；禁止另加值域或其它过滤条件）
- 门禁由 `gate` 一次判定并逐条打印：`<user_id>` 唯一、`<label>` 仅 0/1、列对账（step2_4 vs step2_3_wide_cleaned 列数一致）
- 通过 → 写 `step2_4_user_filter_report.json` + `gate_data_verified_2_4.txt`，并清除 `after_collect`；不通过 → exit 非 0，修正后重跑。下一步执行清单的 `export` 字段

### CSV 导出（`gate` 通过后，独立 todo，不可跳过）

`read_file("skill/feature-engineer/scripts/step2_4_export_csv.md")`，按清单里的 `export` 命令执行：`export-plan --raw-count raw_step2_4_count.json --batch-size 999`（**默认 999**，MCP 单次 collect 结果行数硬上限约 1000；更大值会告警）生成分批 SELECT 清单 → 逐批 `submit_resource_job` / `poll_job` / `collect_job` → **`save-collect --batch` 一次落盘全部批** → 跑清单 `after_collect` 的 `append-csv` 组装 `step2_4_wide_userfiltered.csv`。导出批次由 `export-plan` 一次生成，`save-collect --batch` 一次落盘；**禁止逐批手动 `save-collect`，禁止手写导出 SQL 与手写 CSV。**

### 契约校验（CSV 导出后）

```bash
python skill/feature-engineer/scripts/step2_5_driver.py validate-contract --out-dir .
```

参数由驱动自动预填固定文件名。脚本自行回写 `validation.structural_validation`：对账宽表列、源表 Schema、alias/字段、关系 plan 与 expanded SQL 哈希，并用 cleaning-report 的 DROP 清单核验契约中缺席最终 CSV 的特征。校验失败时先修正契约并重跑，仍失败则不在 receipt 登记该契约。

---

## step2_5：定稿 receipt

按 `skill/feature-engineer/scripts/step2_5_finalize.md` 验收。**定稿唯一入口**为 `step2_5_driver.py finalize`（依次 validate-contract → receipt → verify-all；都执行、失败不中断、汇总退出码）。receipt.json 由脚本从既有产物确定性生成，**禁止手写**。允许单独跑驱动子命令排障，但**禁止**绕过驱动直接调底层脚本：

```bash
python skill/feature-engineer/scripts/step2_5_driver.py finalize --out-dir .

# 单独排障（入口一致，仍走 driver）：
python skill/feature-engineer/scripts/step2_5_driver.py validate-contract --out-dir . [--cleaning-report step2_3_cleaning_report.json] [--enforce]
python skill/feature-engineer/scripts/step2_5_driver.py verify-all --out-dir .
```

`--cleaning-report` / `--report-json` **仅当显式传入**时才前传（不因磁盘上已有文件而自动附加）。`validate-contract --enforce` 缺 cleaning-report 时报错时，重跑 finalize 并补 `--cleaning-report step2_3_cleaning_report.json`。

行数/列数取 `step2_4_wide_userfiltered.csv`，正负比取 `step2_4_user_filter_report.json`，表数取 `schema_resolution.json`。契约条目仅在 `validation.structural_validation.passed=true` 时登记。不写 SQL、不连 `<warehouse>`。

---

## 完成自检清单

`finalize` 已内嵌自检；若需单独复跑，走驱动入口（恒带 `--require-receipt`）：

```bash
python skill/feature-engineer/scripts/step2_5_driver.py verify-all --out-dir .
```

逐项打印 `PASS`/`FAIL`，并写出 `step2_final_check.json`；有 `FAIL` 时 exit 非 0，按打印的条目回到对应阶段修正后重跑。除门禁产物外，还会校验 **CSV 表头列名唯一**，以及 **CSV 表头 SHA256**（`sha256("\x1f".join(header))`）与契约 `wide_csv_header_sha256` **逐位比对**。标记为 `--` 的项须经 `<warehouse>` 查询确认后再提交 receipt。
