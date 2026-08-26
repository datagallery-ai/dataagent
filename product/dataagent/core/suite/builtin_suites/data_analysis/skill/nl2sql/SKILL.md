---
name: nl2sql
description: >-
  比较 LightGBM 演化得到的决策树与评分卡策略，必要时融合两者，并将最终白盒策略转换为一条
  面向全量源数据库的圈选评分 SQL。Use when 需要选择白盒策略、生成最终圈选 SQL 或审计特征血缘。
disable-model-invocation: true
---

# NL2SQL Pipeline（step4_0 + step4_1）

业务阶段固定为：**采样 step1 → 特征工程 step2 → 模型工程 step3 → NL2SQL step4**。

本阶段生成最终 SQL，并在 `source_database` 上执行内部限量 `SELECT` 试算；不提交或执行未经
限量的完整圈选查询。LightGBM 仅作为教师模型参照，不是部署候选。

最终策略只允许为：

```text
decision_tree
scorecard
decision_tree_scorecard_fusion
```

## 改动与数据边界

- 只读取上游已发布产物，不修改 Sampling、Feature Engineering 或 Model Engineering 文件。
- 所有本阶段产物只写当前 workspace。
- 允许在当前 workspace 创建、修改并执行本地 Python 脚本，也允许使用 `python -c` 或
  Python here-doc。
- 将打包的 `step4_0_reconstruct_tree_preprocessing.py` 和
  `step4_1_generate_sql.py` 视为两个职责独立的初始模板。先复制到当前 workspace，
  再按步骤运行并按需修改相应工作副本；不得直接修改共享的打包模板。
- 共享输出区中的上游原件始终只读。需要归一化时保留原始副本，并在当前 workspace
  生成供生成器工作副本读取的输入副本；不得回写共享输出区。
- `source_database` 是最终 SQL 的目标数据库。
- `output_database` 是训练采样库，不得出现在最终 SQL。
- 不提交或执行未经限量的最终圈选 SQL。通过 ClickHouse 资源工具只执行生成器产生的源表
  内部限量试算 SQL；提交给 ClickHouse MCP 的顶层关键字必须是 `SELECT`，不得提交 `EXPLAIN`。
- 不读取 `step3_4_lgb_model.pkl`，不生成 LightGBM SQL。

## 上游输入

从共享 `manifest.json` 中按阶段定位产物，然后复制到当前 workspace 的 `OUTPUT_DIR`。
不要只依赖上一步 receipt 摘要。

### Sampling

- `step1_output_meta.json`
- `step1_sample_stats.json`
- `step1_0_table_schema.json`（存在时仅作旧版一致性核对）

用途：

- 从 `step1_output_meta.json` 获取全量源表、字段、类型、join hints 和用户键别名。
- 从 `step1_sample_stats.json` 获取 `source_database`、`output_database`、`target_game` 和
  `projection_tables[].type`。

### Feature Engineering

- `schema_resolution.json`
- `step2_3_deployment_feature_contract.json`（新版首选且唯一需要流转的特征血缘输入）
- `step2_3_feature_derivation.md`（契约缺失时的旧版兼容输入）
- `step2_3_high_cardinality_check.json`（契约缺失时的旧版兼容输入）
- `step2_3_feature_aggregation_expanded.sql` / `step2_3_feature_aggregation.sql`（旧版兼容时可选）

用途：

- 从 `schema_resolution.json` 获取用户表、用户 ID 和经过验证的关联键。
- 新版契约存在且 `validation.structural_validation.passed=true` 时，必须复核其源表、字段、
  alias、JOIN 和参数，并直接按
  `relation_plans` 渲染；不得再从 Markdown 或 expanded SQL 猜测血缘。
- 新版契约按物理 SQL scope 记录多表 JOIN，因此不得把多个 `source_tables` 推断成
  `UNION ALL`，也不得改写契约中的 alias。
- 新版契约不存在或结构检查未通过时，将 `step2_3_feature_derivation.md` 标准化为
  `step2_3_feature_derivation.json`，并按 expanded SQL → Schema/schema_resolution → Markdown
  的旧版权威顺序兼容历史轨迹。不得根据常识补造表、字段或表达式。

两条路径都会生成 `step2_3_feature_derivation.json` 作为本阶段审计产物；新版路径的 SQL renderer
直接读取已经再次校验的部署契约，不自由解释 Markdown。

### Model Engineering

LightGBM 教师参照：

- `step3_4_valid_predictions.csv`
- `step3_4_model_report.json`

决策树：

- `step3_3_wide_table_train.csv`
- `step3_3_wide_table_valid.csv`
- `step3_3_univariate_analysis.csv`
- `step3_4_feature_importance.csv`
- `step3_5_rule_card.csv`
- `step3_5_white_box_scores.csv`
- `step3_5_model_report.json`

评分卡：

- `step3_6_score_rule.csv`
- `step3_6_white_box_scores.csv`
- `step3_6_model_report.json`

三份验证分数必须按用户一一对齐，label 必须一致。LightGBM 分数只用于教师一致性和效果参照。

新增的四项决策树输入只用于重建历史模型训练时未导出的预处理参数：

- 训练宽表用于按训练时相同规则重建数值特征的 quantile 分箱边界。
- 训练和验证宽表共同用于按训练时相同规则重建类别特征的 `LabelEncoder` 类别顺序。
- 单变量分析提供每个数值特征的唯一值数，从而恢复请求分箱数。
- 特征重要性提供决策树训练时实际选择的 Top-N 特征及顺序。

`step4_0` 必须使用重建后的编码值回放 `step3_5_rule_card.csv`，并与
`step3_5_white_box_scores.csv` 对齐验证。规则分数只保留三位小数，因此默认允许
`0.00051` 的绝对舍入误差。该过程只读取已有产物，不重新训练模型，也不改写任何前序文件。

## 策略选择

统一重算：

- tie-aware Precision / Recall / Lift@K
- AUC、PR-AUC、KS
- 与 LightGBM 的 Spearman 和 AUC gap
- 分数唯一值数、最大并列组和并列率

默认主指标为 `Precision@Top10%`；可通过 `NL2SQL_PRIMARY_K` 调整。

使用固定种子的用户级 paired bootstrap：

- 一个策略显著更好且达到最小相对提升时，直接选择该策略。
- 差异不显著时，按 `hash(user_id)` 拆分 blend-fit / blend-eval，并尝试决策树与评分卡融合。
- 融合采用验证集固定均值/标准差的 z-score 和固定权重网格。
- 融合没有增益时，按主指标、PR-AUC、AUC、教师 Spearman、规则数和 SQL 长度确定性回退。
- 不因 AUC、Spearman、AUC gap、规则数或 Top-K 低而停止正常流程；这些问题写入 `risk_flags`。

先分别检查两个白盒候选的规则、预处理、特征血缘和 SQL 可渲染性，再在技术上可部署的候选
之间比较验证效果。如果只有一个策略具备完整部署信息，则直接使用该策略。

## SQL 生成规则

- 最终只生成 `sql/step4_1_final.sql`。
- 所有表必须限定为 `source_database.<table>`。
- 禁止出现采样 `output_database`。
- 不引用训练宽表。
- 不引用或输出 `label`。
- 不生成 LightGBM、近似 LightGBM或特征重要性加权 SQL。
- 不生成多个候选 SQL。
- 不使用 CTE/`WITH`、`MODE() WITHIN GROUP`、`TRY_TO_NUMERIC`、`INTERVAL`、`LIMIT`。
- 不得存在 `<...>`、`<TBD>` 或其他未解析占位符。
- JOIN 和聚合必须来自 Schema、已验证键和特征血缘，不得补造逻辑表。
- 未提供明确 `selection_rate` 或阈值时输出完整用户评分排序，不自行决定圈选人数。

## ClickHouse 源库验证

静态校验通过后，执行生成器输出的源库限量 `SELECT` 试算 SQL。试算 SQL 在每张物理源表
内部添加 `LIMIT`，再设置时间、线程、读取行数、读取字节和内存上限；禁止只在 final SQL
外层添加 LIMIT。该试算用于验证真实源库中的表、字段、函数、类型、别名和 JOIN。
它是开放式 ClickHouse 表达式语法与执行语义的权威门禁；Step2 固定脚本只检查契约结构。

使用生成器第一次运行产生的 `.nl2sql_runtime/source_validation_request.json`：

- 按其中 `source_trial.path` 和 `source_trial.sha256` 提交唯一的 `source_trial`。
- 固定调用 `submit_resource_job(resource_id="clickhouse", task_type="sql_query",
  command_file=<path>)`，再 `poll_job` 和 `collect_job`。
- 将原始 collect payload 机械写入 `.nl2sql_runtime/source_validation_result.json`，结构为：

```json
{
  "source_trial": {"query_sha256": "<request sha256>", "result": {}}
}
```

再次运行 step4_1，由生成器核对 hash、判定结果并生成 receipt。返回零行属于执行成功；
ClickHouse 语法、字段、函数或类型错误时修改生成器工作副本并重跑。资源限额错误必须在报告
中单独标识，不能冒充 SQL 语法错误。

## 分步脚本与运行方式

生成器按职责拆成两步。不得跳过 `step4_0`，也不得让 `step4_1` 自行猜测分箱边界或类别映射。

### step4_0：重建并验证决策树预处理

使用脚本：`skill/nl2sql/scripts/step4_0_reconstruct_tree_preprocessing.py`

输入：

- `step3_3_wide_table_train.csv`
- `step3_3_wide_table_valid.csv`
- `step3_3_univariate_analysis.csv`
- `step3_4_feature_importance.csv`
- `step3_5_rule_card.csv`
- `step3_5_white_box_scores.csv`
- `step3_5_model_report.json`

输出：

- `step3_5_preprocessing_reconstructed.json`
- `scripts/step4_0_reconstruct_tree_preprocessing.py`

### step4_1：策略选择、血缘渲染与最终 SQL

使用脚本：`skill/nl2sql/scripts/step4_1_generate_sql.py`

除其余上游输入外，必须读取 step4_0 产出的
`step3_5_preprocessing_reconstructed.json`。只有其中的规则回放验证通过且树规则涉及的
特征均存在完整预处理元数据时，决策树才是可部署候选；否则继续使用可部署的评分卡策略，
不得因模型质量门槛停止流程。

```bash
export OUTPUT_DIR="<current workspace>"
export SQL_DIR="${OUTPUT_DIR}/sql"
export NL2SQL_PREPROCESS_TEMPLATE_PATH="skill/nl2sql/scripts/step4_0_reconstruct_tree_preprocessing.py"
export NL2SQL_TEMPLATE_PATH="skill/nl2sql/scripts/step4_1_generate_sql.py"
mkdir -p "${OUTPUT_DIR}/scripts" "${SQL_DIR}"
cp "${NL2SQL_PREPROCESS_TEMPLATE_PATH}" "${OUTPUT_DIR}/scripts/step4_0_reconstruct_tree_preprocessing.py"
cp "${NL2SQL_TEMPLATE_PATH}" "${OUTPUT_DIR}/scripts/step4_1_generate_sql.py"
uv run --no-sync python "${OUTPUT_DIR}/scripts/step4_0_reconstruct_tree_preprocessing.py"
uv run --no-sync python "${OUTPUT_DIR}/scripts/step4_1_generate_sql.py"
```

如果某一步初始模板运行失败，或者结果不符合本 skill 的输入、预处理回放、策略、血缘或
SQL 静态或 ClickHouse 验证要求，可以修改发生问题的对应工作副本后从该步骤重跑。修改时：

- 确认问题来自模板兼容或生成逻辑，而不是通过改写规则、标签或分数规避输入事实。
- 建议在修改 step4_0 时设置 `NL2SQL_PREPROCESS_CHANGE_REASON`，修改 step4_1 时设置
  `NL2SQL_GENERATOR_CHANGE_REASON`，记录修改原因；缺失原因只记审计 warning，不拒绝运行。
- 保持打包模板不变，不手工编辑生成后的 SQL、报告或 receipt。

两个脚本都会记录模板与实际执行脚本的 SHA-256、是否修改和修改原因。实际执行的两个工作
副本都必须作为本阶段产物保留，保证结果可复现。

可选的固定参数：

```bash
export NL2SQL_PRIMARY_K="0.10"
export NL2SQL_BOOTSTRAP_ITERATIONS="500"
export NL2SQL_CONFIDENCE_LEVEL="0.95"
export NL2SQL_MIN_RELATIVE_UPLIFT="0.02"
export NL2SQL_RANDOM_SEED="42"
export NL2SQL_TREE_SCORE_TOLERANCE="0.00051"
```

禁止：

- 修改共享的打包生成器模板。
- 修改共享输出区中的任何上游文件。
- 使用本地 Python 改写模型规则、验证标签、预测分数或策略指标。

修复后应从相应步骤重新生成 SQL、报告和 receipt，避免产物之间不一致；本地辅助 Python
脚本不受文件名和数量限制。

## 最终产物

- `step2_3_feature_derivation.json`
- `step3_5_preprocessing_reconstructed.json`
- `sql/step4_1_final.sql`
- `step4_1_strategy_selection.json`
- `step4_1_feature_lineage_report.json`
- `step4_1_sql_validation_report.json`
- `scripts/step4_0_reconstruct_tree_preprocessing.py`
- `scripts/step4_1_generate_sql.py`
- `receipt.json`

`receipt.json` 仅包含 `summary` 和 `artifacts`，登记六个业务产物和实际执行的两个脚本工作
副本；不把其他上游复制文件登记为本阶段产物。

验证报告必须明确：

```json
{
  "full_database_execution_performed": false,
  "full_database_execution_expected": false,
  "source_trial_validation": {"performed": true, "passed": true}
}
```

不得声称执行了未经限量的最终 SQL。只有源库内部限量 `SELECT` 试算成功后才写
`receipt.json`；`.nl2sql_runtime/` 中的临时 SQL、请求和结果不登记为最终产物。
