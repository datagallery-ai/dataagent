"""
Step 3_4: 模型训练
包含输入校验、特征分层 CONSIDER_DROP 验证、两阶段 A/B 对比、LightGBM 训练与评估
"""

import json
import os
import pickle
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import auc as pr_auc, precision_recall_curve, roc_auc_score, roc_curve
from sklearn.preprocessing import LabelEncoder

try:
    import lightgbm as lgb
    HAS_LGB = True
except ImportError:
    HAS_LGB = False
    print("LightGBM 未安装")

warnings.filterwarnings('ignore')

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


def compute_ks(y_true, y_score):
    """Compute the Kolmogorov-Smirnov statistic from binary labels and predicted scores."""
    fpr, tpr, _ = roc_curve(y_true, y_score)
    return float(np.max(np.abs(tpr - fpr)))


def compute_pr_auc(y_true, y_score):
    """Compute the area under the precision-recall curve."""
    precision, recall, _ = precision_recall_curve(y_true, y_score)
    return float(pr_auc(recall, precision))


def precision_at_k(y_true, y_score, k_pct=0.1):
    """Return the positive rate among the top ``k_pct`` fraction of scores."""
    df = pd.DataFrame({'y': y_true, 'score': y_score}).sort_values('score', ascending=False)
    k = max(1, int(len(df) * k_pct))
    return float(df.head(k)['y'].mean())


print("=" * 60)
print("Step 3_4: 模型训练")
print("=" * 60)

print("\n[3_4_1] 加载数据...")
train_df = pd.read_csv(OUTPUT_DIR / "step3_3_wide_table_train.csv", encoding='utf-8-sig', low_memory=False)
valid_df = pd.read_csv(OUTPUT_DIR / "step3_3_wide_table_valid.csv", encoding='utf-8-sig', low_memory=False)
univariate_df = pd.read_csv(OUTPUT_DIR / "step3_3_univariate_analysis.csv", encoding='utf-8-sig')
print(f"  训练集: {len(train_df)} rows, {len(train_df.columns)} cols")
print(f"  验证集: {len(valid_df)} rows, {len(valid_df.columns)} cols")
print(f"  step3_3 特征: {len(univariate_df)} features")

print("\n[3_4_2] 输入校验...")
train_ids = set(train_df[USER_ID_COL])
valid_ids = set(valid_df[USER_ID_COL])
id_overlap = train_ids & valid_ids
print(f"  user_id 无交集: {len(id_overlap) == 0} (重叠={len(id_overlap)})")

train_pos_rate = train_df[LABEL_COL].mean()
valid_pos_rate = valid_df[LABEL_COL].mean()
pos_rate_diff = abs(train_pos_rate - valid_pos_rate)
print(
    f"  正负样本比例一致: {pos_rate_diff < 0.01} "
    f"(train={train_pos_rate:.4f}, valid={valid_pos_rate:.4f}, diff={pos_rate_diff:.4f})"
)

exclude_cols = [USER_ID_COL, LABEL_COL]
train_feat_cols = [c for c in train_df.columns if c not in exclude_cols]
valid_feat_cols = [c for c in valid_df.columns if c not in exclude_cols]
cols_match = train_feat_cols == valid_feat_cols
print(f"  特征列对齐: {cols_match} (train={len(train_feat_cols)}, valid={len(valid_feat_cols)})")

keep_features = univariate_df[univariate_df['recommendation'] == 'KEEP']['feature'].tolist()
consider_features = univariate_df[univariate_df['recommendation'] == 'CONSIDER_DROP']['feature'].tolist()
print(f"\n  特征分层: KEEP={len(keep_features)}, CONSIDER_DROP={len(consider_features)}")

print("\n[3_4_3] 特征编码...")
all_feat_cols = keep_features + consider_features
X_train = train_df[all_feat_cols].copy()
X_valid = valid_df[all_feat_cols].copy()
y_train = train_df[LABEL_COL].values
y_valid = valid_df[LABEL_COL].values

object_cols = X_train.select_dtypes(include=['object']).columns.tolist()
for col in object_cols:
    le = LabelEncoder()
    all_vals = pd.concat([X_train[col].astype(str), X_valid[col].astype(str)]).fillna('__MISSING__')
    le.fit(all_vals)
    X_train[col] = le.transform(X_train[col].astype(str).fillna('__MISSING__'))
    X_valid[col] = le.transform(X_valid[col].astype(str).fillna('__MISSING__'))

X_train = X_train.astype(float).fillna(-999)
X_valid = X_valid.astype(float).fillna(-999)
print(f"  编码完成: {len(all_feat_cols)} features, {len(object_cols)} object cols")

print("\n[3_4_4] 阶段一：全量训练...")
scale_pos = int((y_train == 0).sum()) / int((y_train == 1).sum())
params = {
    'objective': 'binary',
    'metric': 'auc',
    'boosting_type': 'gbdt',
    'num_leaves': 31,
    'learning_rate': 0.05,
    'feature_fraction': 0.8,
    'bagging_fraction': 0.8,
    'bagging_freq': 5,
    'verbose': -1,
    'seed': RANDOM_STATE,
    'scale_pos_weight': scale_pos
}

train_data = lgb.Dataset(X_train, label=y_train)
valid_data = lgb.Dataset(X_valid, label=y_valid, reference=train_data)

model_full = lgb.train(
    params, train_data,
    num_boost_round=1000,
    valid_sets=[train_data, valid_data],
    valid_names=['train', 'valid'],
    callbacks=[lgb.early_stopping(50), lgb.log_evaluation(100)]
)

gain_importance_full = model_full.feature_importance(importance_type='gain')
importance_df = pd.DataFrame({
    'feature': all_feat_cols,
    'gain_importance': gain_importance_full
}).sort_values('gain_importance', ascending=False).reset_index(drop=True)
importance_df['rank'] = range(1, len(importance_df) + 1)

consider_in_df = importance_df[importance_df['feature'].isin(consider_features)]
consider_median_rank = consider_in_df['rank'].median()
print(f"  CONSIDER_DROP 中位排名: {consider_median_rank:.0f}/{len(all_feat_cols)}")
consider_in_bottom30pct = (consider_in_df['rank'] > 0.7 * len(all_feat_cols)).sum()
print(f"  CONSIDER_DROP 落在后30%: {consider_in_bottom30pct}/{len(consider_in_df)} 个")

strategy_a_features = keep_features
strategy_b_features = all_feat_cols

if consider_median_rank > 0.7 * len(all_feat_cols):
    print("  策略: CONSIDER_DROP 整体排名靠后，直接丢弃")
    strategy_b_features = keep_features
elif len(consider_in_df[consider_in_df['rank'] <= 0.5 * len(all_feat_cols)]) > 0:
    print("  策略: 部分 CONSIDER_DROP 进入前50%，保留这些，其余丢弃")
    keep_consider = consider_in_df[consider_in_df['rank'] <= 0.5 * len(all_feat_cols)]['feature'].tolist()
    strategy_b_features = keep_features + keep_consider
else:
    print("  策略: CONSIDER_DROP 与 KEEP 混排均匀，全部保留")

print("\n[3_4_5] 阶段二：A/B 对比...")


def train_and_evaluate(features, name):
    """Train LightGBM on ``features`` and return the model, validation predictions, and AUC."""
    X_tr = X_train[features].copy()
    X_va = X_valid[features].copy()
    dt = lgb.Dataset(X_tr, label=y_train)
    dv = lgb.Dataset(X_va, label=y_valid, reference=dt)
    m = lgb.train(params, dt, num_boost_round=1000,
                  valid_sets=[dt, dv], valid_names=['train', 'valid'],
                  callbacks=[lgb.early_stopping(50), lgb.log_evaluation(0)])
    pred = m.predict(X_va)
    auc = roc_auc_score(y_valid, pred)
    return m, pred, auc, features

print(f"  实验 A: 仅 KEEP 特征 ({len(strategy_a_features)})")
model_a, pred_a, auc_a, _ = train_and_evaluate(strategy_a_features, 'A')

print(f"  实验 B: 筛选后特征 ({len(strategy_b_features)})")
if strategy_b_features == strategy_a_features:
    model_b, pred_b, auc_b = model_a, pred_a, auc_a
else:
    model_b, pred_b, auc_b, _ = train_and_evaluate(strategy_b_features, 'B')

print(f"  AUC_A={auc_a:.6f}, AUC_B={auc_b:.6f}")
if auc_b >= auc_a + 0.001:
    print(f"  结论: 保留实验B特征集 (AUC提升 {auc_b - auc_a:.6f})")
    final_features = strategy_b_features
    final_model = model_b
    final_pred = pred_b
    final_auc = auc_b
    final_strategy = 'B'
else:
    print(f"  结论: 回退到实验A特征集")
    final_features = strategy_a_features
    final_model = model_a
    final_pred = pred_a
    final_auc = auc_a
    final_strategy = 'A'

print("\n[3_4_6] Baseline 对比 (Logistic Regression)...")
lr = LogisticRegression(max_iter=1000, random_state=RANDOM_STATE)
lr.fit(X_train[final_features], y_train)
lr_pred = lr.predict_proba(X_valid[final_features])[:, 1]
lr_auc = roc_auc_score(y_valid, lr_pred)
print(f"  LR AUC: {lr_auc:.6f}, LGB AUC: {final_auc:.6f}")

print("\n[3_4_7] 评估指标...")
ks = compute_ks(y_valid, final_pred)
pr_auc_val = compute_pr_auc(y_valid, final_pred)
prec_10 = precision_at_k(y_valid, final_pred, 0.1)
prec_20 = precision_at_k(y_valid, final_pred, 0.2)

print(f"  AUC: {final_auc:.6f}")
print(f"  KS: {ks:.4f}")
print(f"  PR-AUC: {pr_auc_val:.4f}")
print(f"  Precision@10%: {prec_10:.4f}")
print(f"  Precision@20%: {prec_20:.4f}")

print("\n[3_4_8] Top-K 效果评估...")
total_pos = int(y_valid.sum())
total_users = len(y_valid)
topk_results = []
for k_pct in [0.01, 0.05, 0.10, 0.20, 0.30, 0.50]:
    k = max(1, int(total_users * k_pct))
    df_sorted = pd.DataFrame({'y': y_valid, 'score': final_pred}).sort_values('score', ascending=False)
    top_k = df_sorted.head(k)
    hit_count = int(top_k['y'].sum())
    precision = float(top_k['y'].mean())
    recall = hit_count / total_pos if total_pos > 0 else 0
    lift = precision / (total_pos / total_users) if total_pos > 0 else 0
    cumulative_gain = hit_count / total_pos if total_pos > 0 else 0
    coverage = k / total_users
    topk_results.append({
        'K': f'Top {int(k_pct*100)}%',
        'n_users': k,
        'precision': round(precision, 4),
        'recall': round(recall, 4),
        'lift': round(lift, 4),
        'hit_count': hit_count,
        'cumulative_gain': round(cumulative_gain, 4),
        'coverage': round(coverage, 4)
    })
    print(
        f"  Top {int(k_pct*100)}% (K={k}): precision={precision:.4f}, "
        f"recall={recall:.4f}, lift={lift:.4f}, hit={hit_count}"
    )

topk_df = pd.DataFrame(topk_results)
topk_df.to_csv(OUTPUT_DIR / "step3_4_topk_evaluation.csv", index=False, encoding='utf-8-sig')

print("\n[3_4_9] 保存结果...")
final_importance = pd.DataFrame({
    'feature': final_features,
    'gain_importance': final_model.feature_importance(importance_type='gain'),
    'split_importance': final_model.feature_importance(importance_type='split')
}).sort_values('gain_importance', ascending=False).reset_index(drop=True)
final_importance.to_csv(OUTPUT_DIR / "step3_4_feature_importance.csv", index=False, encoding='utf-8-sig')

train_result = train_df[[USER_ID_COL, LABEL_COL]].copy()
train_result['score'] = final_model.predict(X_train[final_features])
train_result.to_csv(OUTPUT_DIR / "step3_4_train_predictions.csv", index=False, encoding='utf-8-sig')

valid_result = valid_df[[USER_ID_COL, LABEL_COL]].copy()
valid_result['score'] = final_pred
valid_result.to_csv(OUTPUT_DIR / "step3_4_valid_predictions.csv", index=False, encoding='utf-8-sig')

with open(OUTPUT_DIR / "step3_4_lgb_model.pkl", 'wb') as f:
    pickle.dump(final_model, f)

report = {
    'strategy': final_strategy,
    'final_features_count': len(final_features),
    'keep_features_count': len(keep_features),
    'consider_features_count': len(consider_features),
    'used_consider_count': len([f for f in final_features if f in consider_features]),
    'auc_a': float(auc_a),
    'auc_b': float(auc_b),
    'metrics': {
        'AUC': float(final_auc),
        'KS': float(ks),
        'PR_AUC': float(pr_auc_val),
        'Precision@10%': float(prec_10),
        'Precision@20%': float(prec_20)
    },
    'lr_auc': float(lr_auc),
    'params': params,
    'best_iteration': final_model.best_iteration,
    'topk_evaluation': topk_results
}
with open(OUTPUT_DIR / "step3_4_model_report.json", 'w', encoding='utf-8') as f:
    json.dump(report, f, ensure_ascii=False, indent=2)

print(f"  已保存: step3_4_train_predictions.csv")
print(f"  已保存: step3_4_valid_predictions.csv")
print(f"  已保存: step3_4_feature_importance.csv")
print(f"  已保存: step3_4_lgb_model.pkl")
print(f"  已保存: step3_4_model_report.json")
print(f"  已保存: step3_4_topk_evaluation.csv")

print("\n  Top 20 重要特征:")
print(final_importance.head(20).to_string())

print("\n" + "=" * 60)
print("Step 3_4 完成!")
print("=" * 60)
