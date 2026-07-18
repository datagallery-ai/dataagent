"""
Step 3_5: 白盒打分模型
基于 LightGBM 特征重要性选取 Top 特征，用浅层决策树拟合黑盒分数，
生成可解释的打分卡，在验证集上评估 Top-K 效果
"""

import pandas as pd
import numpy as np
import os
from pathlib import Path
import json
import pickle
import warnings
warnings.filterwarnings('ignore')

from scipy.stats import spearmanr
from sklearn.tree import DecisionTreeRegressor, export_text
from sklearn.preprocessing import LabelEncoder, KBinsDiscretizer
from sklearn.metrics import roc_auc_score

DATA_DIR = Path(os.environ.get("DATA_DIR", ".")).resolve()
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", DATA_DIR / "output")).resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def _require_schema_cols():
    user_id_col = os.environ.get("USER_ID_COL", "").strip()
    label_col = os.environ.get("LABEL_COL", "").strip()
    if not user_id_col or not label_col:
        raise SystemExit(
            "USER_ID_COL and LABEL_COL must be set from schema_resolution (semantic layer); "
            "do not hardcode physical column names."
        )
    return user_id_col, label_col

USER_ID_COL, LABEL_COL = _require_schema_cols()

RANDOM_STATE = 42

print("=" * 60)
print("Step 3_5: 白盒打分模型")
print("=" * 60)

print("\n[3_5_1] 加载数据...")
valid_pred = pd.read_csv(OUTPUT_DIR / "step3_4_valid_predictions.csv", encoding='utf-8-sig')
feature_importance = pd.read_csv(OUTPUT_DIR / "step3_4_feature_importance.csv", encoding='utf-8-sig')
valid_df = pd.read_csv(OUTPUT_DIR / "step3_3_wide_table_valid.csv", encoding='utf-8-sig')
train_pred = pd.read_csv(OUTPUT_DIR / "step3_4_train_predictions.csv", encoding='utf-8-sig')
train_df = pd.read_csv(OUTPUT_DIR / "step3_3_wide_table_train.csv", encoding='utf-8-sig')
univariate_analysis = pd.read_csv(OUTPUT_DIR / "step3_3_univariate_analysis.csv", encoding='utf-8-sig')

print(f"  验证集预测: {len(valid_pred)} rows")
print(f"  特征重要性: {len(feature_importance)} features")

print("\n[3_5_2] 特征筛选（Top30 或累计重要性 > 80%）...")
cum_imp = feature_importance['gain_importance'].cumsum() / feature_importance['gain_importance'].sum()
n_features = min(30, len(feature_importance))
top_features = feature_importance.head(n_features)['feature'].tolist()
print(f"  选取 Top {n_features} 特征: {top_features}")

print("\n[3_5_3] 特征分箱...")
X_train_raw = train_df[top_features].copy()
X_valid_raw = valid_df[top_features].copy()
y_train_score = train_pred['score'].values
y_valid_score = valid_pred['score'].values
y_valid_label = valid_pred[LABEL_COL].values

object_cols = X_train_raw.select_dtypes(include=['object']).columns.tolist()
num_cols = [c for c in top_features if c not in object_cols]

bin_info = {}
X_train_binned = X_train_raw.copy()
X_valid_binned = X_valid_raw.copy()

analysis_map = {row['feature']: row for _, row in univariate_analysis.iterrows()}

for col in num_cols:
    analysis_row = analysis_map.get(col)
    if analysis_row is not None and analysis_row['n_unique'] <= 20:
        bins = KBinsDiscretizer(n_bins=min(5, int(analysis_row['n_unique'])), encode='ordinal', strategy='quantile')
    else:
        bins = KBinsDiscretizer(n_bins=5, encode='ordinal', strategy='quantile')
    notna = ~X_train_raw[col].isna()
    if notna.sum() > 10:
        bins.fit(X_train_raw.loc[notna, [col]])
        X_train_binned[col] = np.nan
        X_valid_binned[col] = np.nan
        X_train_binned.loc[notna, col] = bins.transform(X_train_raw.loc[notna, [col]]).flatten()
        mask_valid = ~X_valid_raw[col].isna()
        X_valid_binned.loc[mask_valid, col] = bins.transform(X_valid_raw.loc[mask_valid, [col]]).flatten()
        bin_info[col] = bins

for col in object_cols:
    le = LabelEncoder()
    all_vals = pd.concat([X_train_raw[col].astype(str), X_valid_raw[col].astype(str)]).fillna('__MISSING__')
    le.fit(all_vals)
    X_train_binned[col] = le.transform(X_train_raw[col].astype(str).fillna('__MISSING__'))
    X_valid_binned[col] = le.transform(X_valid_raw[col].astype(str).fillna('__MISSING__'))

X_train_binned = X_train_binned.astype(float).fillna(-1)
X_valid_binned = X_valid_binned.astype(float).fillna(-1)
print(f"  分箱完成: {len(top_features)} features ({len(num_cols)} numeric, {len(object_cols)} object)")

print("\n[3_5_4] 训练浅层决策树（深度=6）...")
tree_model = DecisionTreeRegressor(
    max_depth=6,
    min_samples_leaf=50,
    random_state=RANDOM_STATE
)
tree_model.fit(X_train_binned, y_train_score)
tree_scores = tree_model.predict(X_valid_binned)

spearman_corr = spearmanr(y_valid_score, tree_scores).correlation
valid_auc = roc_auc_score(y_valid_label, tree_scores)
lgb_auc = roc_auc_score(y_valid_label, y_valid_score)
auc_gap = valid_auc - lgb_auc

print(f"  决策树深度: {tree_model.get_depth()}, 叶子数: {tree_model.get_n_leaves()}")
print(f"  Spearman 相关系数: {spearman_corr:.4f}")
print(f"  白盒 AUC: {valid_auc:.4f}, LGBM AUC: {lgb_auc:.4f}, 差距: {auc_gap:+.4f}")

print("\n[3_5_5] 提取打分规则...")
tree_rules_text = export_text(tree_model, feature_names=top_features, max_depth=6)

def extract_rules_from_tree(tree, feature_names):
    tree_ = tree.tree_
    rules = []

    def recurse(node_id, path_conditions):
        if tree_.children_left[node_id] == -1 and tree_.children_right[node_id] == -1:
            score = float(tree_.value[node_id][0][0])
            rules.append((list(path_conditions), score))
            return
        left_child = tree_.children_left[node_id]
        right_child = tree_.children_right[node_id]
        feature = feature_names[tree_.feature[node_id]]
        threshold = tree_.threshold[node_id]
        left_cond = f"{feature} <= {threshold:.2f}"
        right_cond = f"{feature} > {threshold:.2f}"
        recurse(left_child, path_conditions + [left_cond])
        recurse(right_child, path_conditions + [right_cond])

    recurse(0, [])
    return rules

leaf_rules = extract_rules_from_tree(tree_model, top_features)

rule_id = 0
rule_rows = []
for conditions, score in leaf_rules:
    rule_id += 1
    main_cond = conditions[-1]
    feat_match = [c for c in top_features if c in main_cond]
    feat = feat_match[0] if feat_match else 'unknown'
    rule_rows.append({
        'rule_id': f'R{rule_id}',
        'feature': feat,
        'condition': ' AND '.join(conditions),
        'score': round(score, 3),
        'note': f'叶子节点规则，分值={round(score, 3)}'
    })

for conditions, score in leaf_rules:
    rule_id += 1
    if len(conditions) >= 1:
        main_cond = conditions[-1]
        feat_match = [c for c in top_features if c in main_cond]
        feat = feat_match[0] if feat_match else 'unknown'
        rule_rows.append({
            'rule_id': f'R{rule_id}',
            'feature': feat,
            'condition': ' AND '.join(conditions),
            'score': round(score, 3),
            'note': f'叶子节点规则，分值={round(score, 3)}'
        })

rules_df = pd.DataFrame(rule_rows)
rules_df.to_csv(OUTPUT_DIR / "step3_5_rule_card.csv", index=False, encoding='utf-8-sig')
print(f"  提取 {len(rules_df)} 条规则")
print(f"  规则示例:")
for _, r in rules_df.head(5).iterrows():
    print(f"    {r['rule_id']}: {r['condition']} => score={r['score']}")

print("\n[3_5_6] 验证集 Top-K 评估...")
df = valid_pred.copy()
df['white_box_score'] = tree_scores
df = df.sort_values('white_box_score', ascending=False).reset_index(drop=True)

total_count = len(df)
total_positive = int(df[LABEL_COL].sum())
base_positive_rate = total_positive / total_count

k_percentiles = [0.01, 0.05, 0.10, 0.20, 0.30, 0.50]
results = []

for pct in k_percentiles:
    k = max(1, int(total_count * pct))
    top_k = df.head(k)
    hit_count = int((top_k[LABEL_COL] == 1).sum())
    precision_at_k = hit_count / k if k > 0 else 0
    recall_at_k = hit_count / total_positive if total_positive > 0 else 0
    lift_at_k = precision_at_k / base_positive_rate if base_positive_rate > 0 else 0
    cumulative_gain = hit_count / total_positive if total_positive > 0 else 0
    coverage = k / total_count

    results.append({
        'top_pct': f'{int(pct*100)}%',
        'K': k,
        'n_users': k,
        'precision': f'{precision_at_k:.4f}',
        'recall': f'{recall_at_k:.4f}',
        'lift': f'{lift_at_k:.4f}',
        'hit_count': hit_count,
        'cumulative_gain': f'{cumulative_gain:.4f}',
        'coverage': f'{coverage:.4f}'
    })

    print(f"\n  Top {int(pct*100)}% (K={k}): precision={precision_at_k:.4f}, recall={recall_at_k:.4f}, lift={lift_at_k:.4f}")

results_df = pd.DataFrame(results)
results_df.to_csv(OUTPUT_DIR / "step3_5_topk_evaluation.csv", index=False, encoding='utf-8-sig')

print("\n[3_5_7] 保存结果...")
report = {
    'n_rules': int(len(rules_df)),
    'n_features_used': int(len(top_features)),
    'spearman_corr': float(spearman_corr),
    'valid_auc': float(valid_auc),
    'vs_blackbox_auc_gap': float(auc_gap),
    'tree_depth': int(tree_model.get_depth()),
    'tree_n_leaves': int(tree_model.get_n_leaves()),
    'top_features': [str(f) for f in top_features]
}
with open(OUTPUT_DIR / "step3_5_model_report.json", 'w', encoding='utf-8') as f:
    json.dump(report, f, ensure_ascii=False, indent=2)

valid_result = valid_pred[[USER_ID_COL, LABEL_COL]].copy()
valid_result['white_box_score'] = tree_scores
valid_result.to_csv(OUTPUT_DIR / "step3_5_white_box_scores.csv", index=False, encoding='utf-8-sig')

with open(OUTPUT_DIR / "step3_5_white_box_rules.md", 'w', encoding='utf-8') as f:
    f.write("# Step 3_5: 白盒打分模型规则\n\n")
    f.write(f"## 模型信息\n\n")
    f.write(f"- 规则数: {len(rules_df)}\n")
    f.write(f"- 使用特征数: {len(top_features)}\n")
    f.write(f"- Spearman 相关系数: {spearman_corr:.4f}\n")
    f.write(f"- 白盒 AUC: {valid_auc:.4f}, LGBM AUC: {lgb_auc:.4f}, 差距: {auc_gap:+.4f}\n\n")
    f.write("## 决策树规则\n\n```\n")
    f.write(tree_rules_text)
    f.write("\n```\n\n## 打分卡\n\n")
    f.write(rules_df.to_csv(index=False, encoding='utf-8-sig'))

print(f"  打分卡: step3_5_rule_card.csv ({len(rules_df)} rules)")
print(f"  Top-K评估: step3_5_topk_evaluation.csv")
print(f"  模型报告: step3_5_model_report.json")
print(f"  白盒分数: step3_5_white_box_scores.csv")
print(f"  规则文档: step3_5_white_box_rules.md")

print("\n" + "=" * 60)
print("Step 3_5 完成!")
print("=" * 60)
