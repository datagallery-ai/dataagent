"""
Step 3_6: 白盒评分卡模型（SQL友好兜底方案）
基于特征重要性分层加权 + 单变量 WoE 评分，输出可落库的评分规则
"""

import json
import os
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.metrics import roc_auc_score
from sklearn.preprocessing import KBinsDiscretizer

DATA_DIR = Path(os.environ.get("DATA_DIR", ".")).resolve()
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", DATA_DIR / "output")).resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

warnings.filterwarnings('ignore')


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
print("Step 3_6: 白盒评分卡模型（SQL友好）")
print("=" * 60)

print("\n[3_6_1] 加载数据...")
feature_importance = pd.read_csv(OUTPUT_DIR / "step3_4_feature_importance.csv", encoding='utf-8-sig')
univariate_analysis = pd.read_csv(OUTPUT_DIR / "step3_3_univariate_analysis.csv", encoding='utf-8-sig')
valid_pred = pd.read_csv(OUTPUT_DIR / "step3_4_valid_predictions.csv", encoding='utf-8-sig')
valid_df = pd.read_csv(OUTPUT_DIR / "step3_3_wide_table_valid.csv", encoding='utf-8-sig')

print(f"  特征重要性: {len(feature_importance)} features")
print(f"  step3_3 分析: {len(univariate_analysis)} features")
print(f"  验证集: {len(valid_pred)} rows")

y_valid_label = valid_pred[LABEL_COL].values
y_valid_score = valid_pred['score'].values
baseline_pos_rate = y_valid_label.mean()
print(f"  基准正样本率: {baseline_pos_rate:.4f}")

print("\n[3_6_2] 特征筛选（Top 20% + 重要性分层权重）...")
total_features = len(feature_importance)
N_TOP = int(np.ceil(total_features * 0.20))
WOE_SCALE = 10
SCORE_CLIP_MIN = -30
SCORE_CLIP_MAX = 30

top_feats = feature_importance.head(N_TOP).copy()
top_feats['rank'] = range(1, N_TOP + 1)

thresh_5 = int(np.ceil(total_features * 0.05))
thresh_10 = int(np.ceil(total_features * 0.10))
thresh_15 = int(np.ceil(total_features * 0.15))


def get_weight(rank):
    """Map a feature importance rank to a tiered scorecard weight."""
    if rank <= thresh_5:
        return 1.0
    elif rank <= thresh_10:
        return 0.8
    elif rank <= thresh_15:
        return 0.6
    else:
        return 0.4

top_feats['feature_weight'] = top_feats['rank'].apply(get_weight)
print(f"  总特征数: {total_features}, Top20% = {N_TOP}")
print(f"  阈值: Top5%<=rank{thresh_5} 权重1.0, 5-10% 权重0.8, 10-15% 权重0.6, 15-20% 权重0.4")
print(
    f"  权重分布: 1.0→{(top_feats['rank']<=thresh_5).sum()}, "
    f"0.8→{(top_feats['rank'].between(thresh_5+1, thresh_10)).sum()}, "
    f"0.6→{(top_feats['rank'].between(thresh_10+1, thresh_15)).sum()}, "
    f"0.4→{(top_feats['rank']>thresh_15).sum()}"
)

print("\n[3_6_3] 单变量分箱与 WoE 评分计算...")
rule_rows = []

for _, feat_row in top_feats.iterrows():
    feature = feat_row['feature']
    feat_weight = feat_row['feature_weight']
    rank = feat_row['rank']

    if feature not in valid_df.columns:
        continue

    X_col = valid_df[feature].copy()
    y_col = pd.Series(y_valid_label, index=X_col.index)

    is_string_or_obj = (
        X_col.dtype == 'object'
        or str(X_col.dtype).startswith('string')
        or str(X_col.dtype).startswith('String')
    )
    n_unique = X_col.nunique(dropna=True)

    vals_numeric = pd.to_numeric(X_col, errors='coerce')
    notna_num_mask = ~vals_numeric.isna()
    valid_vals = vals_numeric.loc[notna_num_mask]

    treat_as_string = is_string_or_obj or len(valid_vals) < len(X_col) * 0.3

    if treat_as_string:
        all_vals = X_col.fillna('__MISSING__').astype(str)
        unique_vals = sorted(all_vals.unique())
        bin_values = unique_vals
        is_string_or_obj = True
    else:
        if len(valid_vals) < 2:
            continue

        n_bins_est = min(5, max(2, n_unique))

        if n_unique <= n_bins_est:
            bin_values = sorted(valid_vals.unique())
        else:
            try:
                bins = KBinsDiscretizer(n_bins=n_bins_est, encode='ordinal', strategy='quantile')
                bins.fit(valid_vals.values.reshape(-1, 1))
                n_actual_bins = bins.n_bins_[0]
                bin_edges = bins.bin_edges_[0]
                bin_values = list(range(n_actual_bins))
            except Exception:
                bin_values = sorted(valid_vals.unique())

    for bval in bin_values:
        if is_string_or_obj:
            mask = all_vals == bval
            condition = f"='{bval}'"
        else:
            if len(bin_values) == 1:
                mask = notna_num_mask
                condition = f"={bval:.4f}"
            else:
                idx = list(bin_values).index(bval)
                if idx == 0:
                    mask = (vals_numeric <= bval) & notna_num_mask
                    condition = f"<= {bval:.4f}"
                elif idx == len(bin_values) - 1:
                    left_val = bin_values[idx - 1]
                    mask = (vals_numeric > left_val) & notna_num_mask
                    condition = f"> {left_val:.4f}"
                else:
                    left_val = bin_values[idx - 1]
                    mask = (vals_numeric > left_val) & (vals_numeric <= bval) & notna_num_mask
                    condition = f"> {left_val:.4f} AND <= {bval:.4f}"

        if mask.sum() == 0:
            continue
        total_count = int(mask.sum())
        pos_count = int(y_col[mask].sum())
        neg_count = total_count - pos_count
        pos_rate = pos_count / total_count if total_count > 0 else 0

        if pos_rate > 0 and baseline_pos_rate > 0 and pos_rate < 1:
            woe = np.log(pos_rate / baseline_pos_rate) * WOE_SCALE
        elif pos_rate >= 1:
            woe = WOE_SCALE * 3
        else:
            woe = -WOE_SCALE * 3

        raw_score = round(max(SCORE_CLIP_MIN, min(SCORE_CLIP_MAX, woe)), 4)
        weighted_score = round(raw_score * feat_weight, 4)

        rule_rows.append({
            'feature': feature,
            'rank': int(rank),
            'condition': condition,
            'raw_score': raw_score,
            'weight': feat_weight,
            'weighted_score': weighted_score,
            'pos_rate': round(pos_rate, 6),
            'sample_count': total_count,
            'pos_count': pos_count,
            'neg_count': neg_count,
            'note': f"Rank{int(rank)}_权重{feat_weight}_正率{pos_rate:.4f}"
        })

print(f"  生成 {len(rule_rows)} 条评分规则")

rules_df = pd.DataFrame(rule_rows)

print("\n[3_6_4] 计算白盒评分...")
valid_scores = np.full(len(valid_df), 0.0)

for _, r in rules_df.iterrows():
    feat = r['feature']
    cond = r['condition']
    w_score = r['weighted_score']

    col = valid_df[feat]
    is_obj_type = col.dtype == 'object' or str(col.dtype).startswith('string') or str(col.dtype).startswith('String')

    if is_obj_type:
        str_val = cond.split("='")[1].strip("'") if "='" in cond else cond.split('=')[1].strip("'")
        match_mask = (col.fillna('__MISSING__').astype(str) == str_val)
    else:
        col_num = pd.to_numeric(col, errors='coerce')
        if '<=' in cond and '>' in cond:
            parts = cond.replace(' AND ', ' ').split()
            gt_idx = next(i for i, p in enumerate(parts) if p == '>')
            le_idx = next(i for i, p in enumerate(parts) if p == '<=')
            gt_val = float(parts[gt_idx + 1])
            le_val = float(parts[le_idx + 1])
            match_mask = (col_num > gt_val) & (col_num <= le_val)
        elif '<=' in cond:
            le_val = float(cond.split('<=')[1].strip())
            match_mask = col_num <= le_val
        elif '>' in cond:
            gt_val = float(cond.split('>')[1].strip())
            match_mask = col_num > gt_val
        elif '=' in cond:
            val_str = cond.split('=')[1].strip("'")
            if val_str == '__MISSING__':
                match_mask = col.isna()
            else:
                try:
                    val = float(val_str)
                    match_mask = col == val
                except ValueError:
                    match_mask = pd.Series(False, index=col.index)
        else:
            continue

    valid_scores[match_mask.values] += w_score

spearman_corr = spearmanr(y_valid_score, valid_scores).correlation
valid_auc = roc_auc_score(y_valid_label, valid_scores)
lgb_auc = roc_auc_score(y_valid_label, y_valid_score)
tree_report_path = OUTPUT_DIR / "step3_5_model_report.json"
if tree_report_path.exists():
    with open(tree_report_path, 'r', encoding='utf-8') as f:
        tree_report = json.load(f)
    tree_auc = tree_report['valid_auc']
    tree_auc_gap = valid_auc - tree_auc
else:
    tree_auc = None
    tree_auc_gap = None
auc_gap = valid_auc - lgb_auc

print(f"  Spearman 相关系数: {spearman_corr:.4f}")
print(f"  白盒 AUC: {valid_auc:.4f}, LGBM AUC: {lgb_auc:.4f}, 差距: {auc_gap:+.4f}")
if tree_auc:
    print(f"  vs 决策树 AUC: {tree_auc:.4f}, 差距: {tree_auc_gap:+.4f}")

print("\n[3_6_5] Top-K 评估...")
df = valid_pred[[USER_ID_COL, LABEL_COL]].copy()
df['white_box_score'] = valid_scores
df = df.sort_values('white_box_score', ascending=False).reset_index(drop=True)

total_count = len(df)
total_positive = int(df[LABEL_COL].sum())
base_positive_rate = total_positive / total_count

k_percentiles = [0.01, 0.05, 0.10, 0.20, 0.30, 0.50]
topk_results = []

for pct in k_percentiles:
    k = max(1, int(total_count * pct))
    top_k = df.head(k)
    hit_count = int((top_k[LABEL_COL] == 1).sum())
    precision_at_k = hit_count / k if k > 0 else 0
    recall_at_k = hit_count / total_positive if total_positive > 0 else 0
    lift_at_k = precision_at_k / base_positive_rate if base_positive_rate > 0 else 0
    cumulative_gain = hit_count / total_positive if total_positive > 0 else 0
    coverage = k / total_count

    topk_results.append({
        'K': k,
        'n_users': k,
        'precision': round(precision_at_k, 4),
        'recall': round(recall_at_k, 4),
        'lift': round(lift_at_k, 4),
        'hit_count': hit_count,
        'cumulative_gain': round(cumulative_gain, 4),
        'coverage': round(coverage, 4)
    })
    print(
        f"  Top {int(pct*100)}% (K={k}): precision={precision_at_k:.4f}, "
        f"recall={recall_at_k:.4f}, lift={lift_at_k:.4f}"
    )

topk_df = pd.DataFrame(topk_results)

print("\n[3_6_6] 保存结果...")
score_rules = rules_df[['feature', 'condition', 'raw_score', 'weight', 'weighted_score',
                          'pos_rate', 'sample_count']].copy()
score_rules.columns = ['feature', 'condition', 'raw_score', 'weight', 'weighted_score',
                         'pos_rate', 'sample_count']
score_rules['source_table'] = 'step3_3_wide_table'
score_rules['source_field'] = score_rules['feature']
score_rules['note'] = rules_df['note']
score_rules = score_rules[['feature', 'condition', 'raw_score', 'weight', 'weighted_score',
                               'pos_rate', 'sample_count', 'source_table', 'source_field', 'note']]
score_rules.to_csv(OUTPUT_DIR / "step3_6_score_rule.csv", index=False, encoding='utf-8-sig')
print(f"  评分规则: step3_6_score_rule.csv ({len(score_rules)} rules)")

topk_df.to_csv(OUTPUT_DIR / "step3_6_topk_evaluation.csv", index=False, encoding='utf-8-sig')
print(f"  Top-K评估: step3_6_topk_evaluation.csv")

report = {
    'n_features': int(N_TOP),
    'n_rules': len(rules_df),
    'spearman_corr': float(spearman_corr),
    'valid_auc': float(valid_auc),
    'vs_blackbox_auc_gap': float(auc_gap),
    'vs_tree_auc_gap': float(tree_auc_gap) if tree_auc_gap is not None else None,
    'baseline_pos_rate': float(baseline_pos_rate),
    'lgb_auc': float(lgb_auc),
    'tree_auc': float(tree_auc) if tree_auc else None,
    'woe_scale': WOE_SCALE,
    'score_clip': [SCORE_CLIP_MIN, SCORE_CLIP_MAX],
    'weight_tiers': {
        f'top{int(thresh_5/total_features*100)}%': 1.0,
        f'{int(thresh_5/total_features*100)}_{int(thresh_10/total_features*100)}%': 0.8,
        f'{int(thresh_10/total_features*100)}_{int(thresh_15/total_features*100)}%': 0.6,
        f'{int(thresh_15/total_features*100)}_20%': 0.4
    }
}
with open(OUTPUT_DIR / "step3_6_model_report.json", 'w', encoding='utf-8') as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print(f"  模型报告: step3_6_model_report.json")

valid_result = valid_pred[[USER_ID_COL, LABEL_COL]].copy()
valid_result['white_box_score'] = valid_scores
valid_result.to_csv(OUTPUT_DIR / "step3_6_white_box_scores.csv", index=False, encoding='utf-8-sig')
print(f"  白盒分数: step3_6_white_box_scores.csv")

print("\n" + "=" * 60)
print("Step 3_6 完成!")
print("=" * 60)
