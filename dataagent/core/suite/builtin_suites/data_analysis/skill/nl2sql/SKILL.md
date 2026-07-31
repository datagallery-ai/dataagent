---
name: nl2sql
description: >-
  比较 LightGBM 演化得到的决策树与评分卡策略，必要时融合两者，并将最终白盒策略转换为一条
  面向全量源数据库的圈选评分 SQL。Use when 需要选择白盒策略、生成最终圈选 SQL 或审计特征血缘。
disable-model-invocation: true
---

# NL2SQL Pipeline（step4_0 + step4_1）

业务阶段固定为：**采样 step1 → 特征工程 step2 → 模型工程 step3 → NL2SQL step4**。

本阶段只生成 SQL，不执行全量数据库查询。LightGBM 仅作为教师模型参照，不是部署候选。

最终策略只允许为：

```text
decision_tree
scorecard
decision_tree_scorecard_fusion
```

## 改动与数据边界

- 只读取上游已发布产物，不修改 Sampling、Feature Engineering 或 Model Engineering 文件。
- 所有本阶段产物只写当前 workspace。
- 允许在当前 workspace 创建并执行本地 Python 脚本，也允许使用 `python -c` 或
  Python here-doc 进行文件检查、格式归一化、特征血缘整理和静态验证。
- 将打包的 `step4_0_reconstruct_tree_preprocessing.py` 和
  `step4_1_generate_sql.py` 视为两个职责独立的初始模板。先复制到当前 workspace，
  再按步骤运行并按需修改相应工作副本；不得直接修改共享的打包模板。
- 本地 Python 不得连接数据库或其他外部资源，不得通过 `submit_resource_job` 等资源工具
  提交 Python 代码。该限制由 governance 同时强制执行。
- 共享输出区中的上游原件始终只读。需要归一化时保留原始副本，并在当前 workspace
  生成供生成器工作副本读取的输入副本；不得回写共享输出区。
- `source_database` 是最终 SQL 的目标数据库。
- `output_database` 是训练采样库，不得出现在最终 SQL。
- 不提交或执行最终圈选 SQL；本阶段只进行本地生成和静态验证。
- 不读取 `step3_4_lgb_model.pkl`，不生成 LightGBM SQL。

## 上游输入

从共享 `manifest.json` 中按阶段定位产物，然后复制到当前 workspace 的 `OUTPUT_DIR`。
不要只依赖上一步 receipt 摘要。

### Sampling

- `step1_0_table_schema.json`
- `step1_output_meta.json`

用途：

- 从 `step1_0_table_schema.json` 获取全量源表、字段和类型。
- 从 `step1_output_meta.json` 获取 `source_database`、`output_database` 和表清单。

### Feature Engineering

- `schema_resolution.json`
- `step2_3_feature_derivation.md`
- `step2_3_high_cardinality_check.json`
- `step2_3_feature_aggregation.sql`（上游已发布时作为可选的血缘核对输入）

用途：

- 从 `schema_resolution.json` 获取用户表、用户 ID 和经过验证的关联键。
- 将 `step2_3_feature_derivation.md` 在当前 workspace 中标准化为
  `step2_3_feature_derivation.json`。
- `step2_3_high_cardinality_check.json` 用于审计分箱、映射和列表特征处理。
- Markdown 信息不足时，只能使用已发布的 `step2_3_feature_aggregation.sql`、Schema
  和高基数检查结果补全血缘；不得根据常识补造表、字段或表达式。

后续 SQL renderer 只读取生成后的 `step2_3_feature_derivation.json`，不直接自由解释 Markdown。

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

如果只有一个策略具备完整规则、特征血缘和部署预处理信息，则直接使用该策略。

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
python3 "${OUTPUT_DIR}/scripts/step4_0_reconstruct_tree_preprocessing.py"
python3 "${OUTPUT_DIR}/scripts/step4_1_generate_sql.py"
```

如果某一步初始模板运行失败，或者结果不符合本 skill 的输入、预处理回放、策略、血缘或
SQL 静态校验要求，可以修改发生问题的对应工作副本后从该步骤重跑。修改前必须：

- 确认问题来自模板兼容或生成逻辑，而不是通过改写规则、标签或分数规避输入事实。
- 修改 step4_0 时设置 `NL2SQL_PREPROCESS_CHANGE_REASON`；修改 step4_1 时设置
  `NL2SQL_GENERATOR_CHANGE_REASON`，简要记录修改原因。
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
- 手工修改生成后的 SQL。
- 手写策略选择、血缘或 SQL 验证报告。
- 使用两个规定工作副本以外的脚本直接生成或覆盖预处理重建结果、最终 SQL、策略报告、
  血缘报告、SQL 验证报告或 `receipt.json`。
- 通过 `submit_resource_job` 或其他外部资源通道发送、执行 Python 代码。

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
  "full_database_execution_expected": false
}
```

不得声称已经在 ClickHouse 或全量数据库执行最终 SQL。
