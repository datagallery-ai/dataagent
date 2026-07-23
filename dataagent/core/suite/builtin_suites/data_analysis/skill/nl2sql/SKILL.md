---
name: nl2sql
description: >-
  比较 LightGBM 演化得到的决策树与评分卡策略，必要时融合两者，并将最终白盒策略转换为一条
  面向全量源数据库的圈选评分 SQL。Use when 需要选择白盒策略、生成最终圈选 SQL 或审计特征血缘。
disable-model-invocation: true
---

# NL2SQL Pipeline（step4_1）

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
- 所有本阶段产物只写当前 job workspace。
- `source_database` 是最终 SQL 的目标数据库。
- `output_database` 是训练采样库，不得出现在最终 SQL。
- 不提交或执行最终圈选 SQL；本阶段只进行本地生成和静态验证。
- 不读取 `step3_4_lgb_model.pkl`，不生成 LightGBM SQL。

## 上游输入

从共享 `manifest.json` 中按阶段定位产物，然后复制到当前 job workspace 的 `OUTPUT_DIR`。
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

用途：

- 从 `schema_resolution.json` 获取用户表、用户 ID 和经过验证的关联键。
- 将 `step2_3_feature_derivation.md` 在当前 workspace 中标准化为
  `step2_3_feature_derivation.json`。
- `step2_3_high_cardinality_check.json` 用于审计分箱、映射和列表特征处理。

后续 SQL renderer 只读取生成后的 `step2_3_feature_derivation.json`，不直接自由解释 Markdown。

### Model Engineering

LightGBM 教师参照：

- `step3_4_valid_predictions.csv`
- `step3_4_model_report.json`

决策树：

- `step3_5_rule_card.csv`
- `step3_5_white_box_scores.csv`
- `step3_5_model_report.json`

评分卡：

- `step3_6_score_rule.csv`
- `step3_6_white_box_scores.csv`
- `step3_6_model_report.json`

三份验证分数必须按用户一一对齐，label 必须一致。LightGBM 分数只用于教师一致性和效果参照。

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

如果只有一个策略的规则和特征能够完整转换为 SQL，则直接使用该策略。

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

## 固定运行方式

```bash
export OUTPUT_DIR="<current job workspace>"
export SQL_DIR="${OUTPUT_DIR}/sql"
python3 skill/nl2sql/scripts/step4_1_generate_sql.py
```

可选的固定参数：

```bash
export NL2SQL_PRIMARY_K="0.10"
export NL2SQL_BOOTSTRAP_ITERATIONS="500"
export NL2SQL_CONFIDENCE_LEVEL="0.95"
export NL2SQL_MIN_RELATIVE_UPLIFT="0.02"
export NL2SQL_RANDOM_SEED="42"
```

禁止：

- 复制生成器创建临时 fixed 版本。
- 修改打包的生成器。
- 修改任何上游文件。
- 手工修改生成后的 SQL。
- 手写策略选择、血缘或 SQL 验证报告。
- 使用 Python here-doc 或 `python -c` 重写本阶段逻辑。

## 最终产物

- `step2_3_feature_derivation.json`
- `sql/step4_1_final.sql`
- `step4_1_strategy_selection.json`
- `step4_1_feature_lineage_report.json`
- `step4_1_sql_validation_report.json`
- `receipt.json`

`receipt.json` 仅包含 `summary` 和 `artifacts`，登记上述五个业务产物；不把上游复制文件登记为本阶段产物。

验证报告必须明确：

```json
{
  "full_database_execution_performed": false,
  "full_database_execution_expected": false
}
```

不得声称已经在 ClickHouse 或全量数据库执行最终 SQL。
