---
name: nl2sql
description: >-
  将 LightGBM、决策树白盒规则和加权评分卡转换为可部署的无 CTE 圈选 SQL，并复现上游特征衍生。
  Use when 需要把模型或评分规则生成 SQL、部署圈选逻辑、审计 SQL 与训练特征一致性。
disable-model-invocation: true
---

# NL2SQL Pipeline（step4_1）

业务阶段固定为：**采样 step1 → 特征工程 step2 → 模型工程 step3 → NL2SQL step4**。本 skill
承接最终 SQL 生成。算子为 `skill/nl2sql/scripts/step4_1_generate_sql.py`；产物前缀 **`step4_1_`**。

## 输入

上游来自模型工程的已发布 manifest entry（非 ClickHouse 训练宽表）：

- `schema_resolution.json`
- `step2_4_feature_derivation.md`
- `step3_4_feature_importance.csv`
- `step3_5_rule_card.csv`
- `step3_6_score_rule.csv`
- `step3_4_lgb_model.pkl`
- `step3_4_model_report.json`

校验可对齐：`step3_4_valid_predictions.csv`、`step3_5_white_box_scores.csv`、
`step3_6_white_box_scores.csv`。

输入缺失、规则无法解析或特征无法还原时停止；不得用恒定分数骨架 SQL 冒充完成。
执行前必须将上述全部文件从所选 manifest entry 复制到当前 job workspace 的 `OUTPUT_DIR`。

## 硬约束

- 表名、列名和连接键只来自 `schema_resolution.json`。
- 衍生字段复现 `step2_4_feature_derivation.md`。
- SQL 禁止 CTE/`WITH`、`MODE() WITHIN GROUP`、`TRY_TO_NUMERIC`、`INTERVAL`、`LIMIT`。
- 不向 ClickHouse 提交 Python；可用 MCP 小样本试跑生成后的部署 SQL。禁止使用 `EXPLAIN`。
- 不得交付 `step10_*`、`step11_*`、`Step4_*` 或无前缀 SQL。

## 运行

```bash
export OUTPUT_DIR="<current job workspace>"
export SQL_DIR="${OUTPUT_DIR}/sql"
python3 skill/nl2sql/scripts/step4_1_generate_sql.py
```

## 生成与校验

产出：

- `sql/step4_1_feature_derivation.sql`
- `sql/step4_1_lgb_model.sql`
- `sql/step4_1_decision_tree.sql`
- `sql/step4_1_scorecard.sql`
- `step4_1_sql_validation_report.json`
- `step4_1_join_assumptions.md`

`receipt.json` 登记全部 `step4_1_*` 产物。
