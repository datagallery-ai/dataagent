---
name: model-engineer
description: >-
  对游戏用户宽表完成分层切分、单变量筛选、LightGBM 训练、模型评估，以及决策树和加权评分卡
  两类白盒模型构建。Use when 需要特征筛选、二分类训练、Top-K 评估、模型解释或评分规则。
disable-model-invocation: true
---

# Model Engineering Pipeline（step3_1–step3_6）

业务阶段固定为：**采样 step1 → 特征工程 step2 → 模型工程 step3 → NL2SQL step4**。本 skill
承接训练集切分至两类白盒模型；**唯一 tabular 输入**为  
`OUTPUT_DIR/step2_4_wide_userfiltered.csv`。部署 SQL 由下游 `step4_1` 生成。

## 完成标准

- 模型工程算子固定为 `scripts/run_step3_pipeline.sh`（Bash 编排）调用六个已版本化
  `step3_*.py`；必须按 `step3_1` → `step3_6` 全部执行。
- **运行期间禁止创建、复制、生成、覆盖或编辑任何 `.py` 文件。** 不得临时生成 Python
  算子、`python -c`、here-doc Python、Notebook 转 Python 或下载 Python 脚本。
- 运行参数只能通过 Bash 环境变量调整；不得在任务运行时修改 Python 源码。
- **唯一 tabular 输入**：`OUTPUT_DIR/step2_4_wide_userfiltered.csv`。不得读取
  `step2_2` / `step2_3` / `step2_4` 宽表，也不得从 ClickHouse 拉训练宽表。
- **纯本地数据平面**：模型工程不得以任何方式连接 ClickHouse。禁止 ClickHouse MCP、
  Bash/shell 客户端、`curl`、数据库驱动和共享 ClickHouse I/O helper；不得创建、执行或提交
  SQL 脚本及 SQL 语句。
- Python 直接以 `pd.read_csv` 读取最终 FE CSV；tabular 中间/最终产物写本地
  `step3_x_*.csv`，json / md / pkl 也只写当前 job workspace 的同一 `OUTPUT_DIR`。
- `<user_id>`、`<label>` 经 `USER_ID_COL`、`LABEL_COL` 注入；禁止硬编码列名。
- 固定随机种子；报告记录依赖、摘要、参数、特征清单、种子。
- 不生成部署 SQL。
- 输入缺失或门禁失败即停止；不得伪造完成态 `receipt.json`。

## 输入：FE handoff

workflow 从 feature_engineering receipt 传入：

| artifact | 用途 |
|----------|------|
| `schema_resolution.json` | 解析 `USER_ID_COL`、`LABEL_COL` |
| `step2_4_wide_userfiltered.csv` | 唯一训练数据 |

开工前从只读共享产物区的 `manifest.json` 中定位特征工程阶段已发布的文件，并将所需文件复制到当前 job workspace 的 `OUTPUT_DIR`。

## 数据平面：本地 workspace

```bash
export OUTPUT_DIR="<current job workspace>"
export USER_ID_COL="<schema_resolution.roles.user_id>"
export LABEL_COL="<schema_resolution.roles.label>"
export SCHEMA_RESOLUTION_PATH="${OUTPUT_DIR}/schema_resolution.json"
```

| 类型 | 位置 |
|------|------|
| 唯一训练输入宽表 | `OUTPUT_DIR/step2_4_wide_userfiltered.csv` |
| 切分/筛选/预测/规则/评分 tabular | `OUTPUT_DIR/step3_x_*.csv` |
| 报告 json / 规则 md / 模型 pkl | `OUTPUT_DIR` |

`DATABASE` 不得作为模型工程输入。固定算子只用 `pd.read_csv` / `to_csv` 读写上述本地文件，
不得读取或写入当前 job workspace 之外的位置。

## 运行

```bash
export OUTPUT_DIR="<current job workspace>"
export USER_ID_COL="..."
export LABEL_COL="..."
export PYTHON_BIN="python3"
export STEP_START="1"
export STEP_END="6"
bash scripts/run_step3_pipeline.sh
```

Bash 强制预检 `step2_4_wide_userfiltered.csv` 存在且非空；执行前后校验固定 Python 文件集合与
SHA-256。局部重跑仅改 `STEP_START` / `STEP_END`。

## step3_1：分层切分训练/验证集

执行 `step3_1_y_label_split.py`，读取 `step2_4_wide_userfiltered.csv`。

1. `LABEL_COL` 仅 0/1；1 正、0 负。`USER_ID_COL` 非空。
2. 以用户为切分单位；同一用户不跨集合。
3. 正负各自 80%/20% 固定种子切分后合并打乱。
4. train/valid 用户交集为空。

产出：`step3_1_wide_table_train.csv`、`step3_1_wide_table_valid.csv`、`step3_1_split_report.json`

## step3_2：单变量初筛

执行 `step3_2_univariate_screening.py`。训练集定箱；验证集复用同一规则。

计算 coverage、ratio、sample_pos_rate、n_unique、missing_rate、IV、PSI。  
`missing_rate>0.5` 或 `n_unique==1` → DROP；否则 `IV<0.02` 或 `PSI>0.2` → CONSIDER_DROP；其余 KEEP。

汇总 7 列：`feature,n_unique,missing_rate,iv,psi,recommendation,reason`。

产出：`step3_2_univariate_analysis.csv`、`step3_2_univariate_analysis_full.json`

## step3_3：训练前过滤

执行 `step3_3_feature_filter.py`。去掉 DROP；保留 KEEP + CONSIDER_DROP。

产出：`step3_3_wide_table_train.csv`、`step3_3_wide_table_valid.csv`、
`step3_3_univariate_analysis.csv`、`step3_3_filter_report.json`

## step3_4：LightGBM 黑盒模型

执行 `step3_4_train_model.py`。输入校验：用户无交集、正负比例差 `<0.01`、特征列对齐。

CONSIDER_DROP 两阶段 + A/B（阈值与现网一致）。主模型 LightGBM；Top-K 评估。

产出：`step3_4_train_predictions.csv`、`step3_4_valid_predictions.csv`、
`step3_4_feature_importance.csv`、`step3_4_lgb_model.pkl`、`step3_4_model_report.json`、
`step3_4_topk_evaluation.csv`

## step3_5：决策树白盒打分模型

执行 `step3_5_white_box_model.py`。拟合黑盒 score；目标 Spearman `>0.9`。

产出：`step3_5_rule_card.csv`、`step3_5_topk_evaluation.csv`、`step3_5_model_report.json`、
`step3_5_white_box_scores.csv`、`step3_5_white_box_rules.md`

## step3_6：加权评分卡白盒模型

执行 `step3_6_white_box_scorecard_model.py`。Top 20% 分档权重；目标 Spearman `>0.8`、
黑盒 AUC gap `<0.05`。

产出：`step3_6_score_rule.csv`、`step3_6_topk_evaluation.csv`、`step3_6_model_report.json`、
`step3_6_white_box_scores.csv`

## receipt 登记

receipt 仅登记 NL2SQL 下游所需文件，缺一不可：

| artifact | 来源 | 用途 |
|----------|------|------|
| `schema_resolution.json` | FE handoff | 解析 `USER_ID_COL`、`LABEL_COL` |
| `step3_4_feature_importance.csv` | step3_4 | LightGBM 特征权重 |
| `step3_5_rule_card.csv` | step3_5 | 决策树规则 |
| `step3_6_score_rule.csv` | step3_6 | 评分卡规则 |
| `step3_4_lgb_model.pkl` | step3_4 | LightGBM 模型文件 |
| `step3_4_model_report.json` | step3_4 | 模型元信息（阈值、AUC 等） |
