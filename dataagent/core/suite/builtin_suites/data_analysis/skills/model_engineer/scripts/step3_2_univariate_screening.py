"""
Step 3_2: 单变量初筛
对每个特征取值或分桶，计算:
- count_pos, count_neg
- pos_coverage = P(value | y=1)
- neg_coverage = P(value | y=0)
- ratio = pos_coverage / neg_coverage（当neg_coverage不为0时）
- sample_pos_rate = P(y=1 | value)
- n_unique
- Information Value（当pos_coverage不为0且neg_coverage不为0时）
- train/valid 稳定性（PSI方法）
输入: OUTPUT_DIR/step3_1_wide_table_train.csv, OUTPUT_DIR/step3_1_wide_table_valid.csv
输出: OUTPUT_DIR/step3_2_univariate_analysis.csv, OUTPUT_DIR/step3_2_univariate_analysis_full.json
"""

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.preprocessing import KBinsDiscretizer

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


PSI_BINS = 10


def compute_psi(train_series: pd.Series, valid_series: pd.Series, bins: int = PSI_BINS) -> float:
    """
    计算PSI（Population Stability Index）
    PSI = Σ ((Actual% - Expected%) × ln(Actual% / Expected%))
    """
    try:
        train_notna = train_series.dropna()
        valid_notna = valid_series.dropna()

        if len(train_notna) == 0 or len(valid_notna) == 0:
            return np.nan

        if train_notna.dtype in ['object', 'string'] or not np.issubdtype(train_notna.dtype, np.number):
            all_vals = sorted(set(train_notna.unique()) | set(valid_notna.unique()))
            if len(all_vals) > 100:
                return np.nan
            train_pct = train_notna.value_counts(normalize=True, dropna=False).reindex(all_vals, fill_value=0.001)
            valid_pct = valid_notna.value_counts(normalize=True, dropna=False).reindex(all_vals, fill_value=0.001)
        else:
            try:
                kb = KBinsDiscretizer(n_bins=bins, encode='ordinal', strategy='quantile')
                all_data = pd.concat([train_notna, valid_notna]).values.reshape(-1, 1)
                kb.fit(all_data)
                train_bins = kb.transform(train_notna.values.reshape(-1, 1)).flatten()
                valid_bins = kb.transform(valid_notna.values.reshape(-1, 1)).flatten()
            except:
                return np.nan

            train_counts = (
                pd.Series(train_bins)
                .value_counts(normalize=True, sort=False)
                .reindex(range(bins), fill_value=0.001)
            )
            valid_counts = (
                pd.Series(valid_bins)
                .value_counts(normalize=True, sort=False)
                .reindex(range(bins), fill_value=0.001)
            )
            train_pct = train_counts
            valid_pct = valid_counts

        train_pct = train_pct.replace(0, 0.001)
        valid_pct = valid_pct.replace(0, 0.001)

        psi = np.sum((valid_pct - train_pct) * np.log(valid_pct / train_pct))
        return float(psi)
    except Exception:
        return np.nan


def analyze_categorical_feature(series: pd.Series, pos_mask: pd.Series, neg_mask: pd.Series,
                                  total_pos: int, total_neg: int) -> dict:
    """分析枚举值较少的特征（n_unique <= 100）"""
    results = {
        'n_unique': int(series.nunique()),
        'missing_rate': float(series.isna().sum() / len(series)),
        'value_stats': [],
        'IV': None
    }

    total_count = len(series)

    for value in series.unique():
        is_value = series == value
        group_pos = int(is_value[pos_mask].sum())
        group_neg = int(is_value[neg_mask].sum())
        group_all = int(is_value.sum())

        pos_cov = group_pos / total_pos if total_pos > 0 else 0.0
        neg_cov = group_neg / total_neg if total_neg > 0 else 0.0
        ratio = pos_cov / neg_cov if neg_cov > 0 else np.inf
        sample_pos_rate = group_pos / group_all if group_all > 0 else 0.0

        results['value_stats'].append({
            'value': str(value) if not pd.isna(value) else 'NULL',
            'count': int(group_all),
            'count_pos': int(group_pos),
            'count_neg': int(group_neg),
            'pos_coverage': float(pos_cov),
            'neg_coverage': float(neg_cov),
            'ratio': float(ratio) if ratio != np.inf else 'inf',
            'sample_pos_rate': float(sample_pos_rate)
        })

    if series.isna().sum() > 0:
        is_na = series.isna()
        group_pos = int(is_na[pos_mask].sum())
        group_neg = int(is_na[neg_mask].sum())
        group_all = int(is_na.sum())
        pos_cov = group_pos / total_pos if total_pos > 0 else 0.0
        neg_cov = group_neg / total_neg if total_neg > 0 else 0.0
        ratio = pos_cov / neg_cov if neg_cov > 0 else np.inf
        sample_pos_rate = group_pos / group_all if group_all > 0 else 0.0
        results['value_stats'].append({
            'value': 'NULL',
            'count': int(group_all),
            'count_pos': int(group_pos),
            'count_neg': int(group_neg),
            'pos_coverage': float(pos_cov),
            'neg_coverage': float(neg_cov),
            'ratio': float(ratio) if ratio != np.inf else 'inf',
            'sample_pos_rate': float(sample_pos_rate)
        })

    iv_total = 0.0
    for stat in results['value_stats']:
        pos_cov = stat['pos_coverage']
        neg_cov = stat['neg_coverage']
        if pos_cov > 0 and neg_cov > 0:
            woe = np.log(pos_cov / neg_cov)
            iv = (pos_cov - neg_cov) * woe
            iv_total += iv
    results['IV'] = float(iv_total) if iv_total > 0 else 0.0

    return results


def analyze_numeric_feature(series: pd.Series, pos_mask: pd.Series, neg_mask: pd.Series,
                             total_pos: int, total_neg: int, bins: int = 10) -> dict:
    """分析连续数值特征（自动分箱）"""
    results = {
        'n_unique': int(series.nunique()),
        'missing_rate': float(series.isna().sum() / len(series)),
        'value_stats': [],
        'IV': None
    }

    try:
        notna_mask = ~series.isna()
        if notna_mask.sum() < bins:
            return results

        kb = KBinsDiscretizer(n_bins=bins, encode='ordinal', strategy='quantile')
        binned = np.full(len(series), np.nan)
        binned[notna_mask] = kb.fit_transform(series[notna_mask].values.reshape(-1, 1)).flatten()

        binned_series = pd.Series(binned, index=series.index)

        for bin_val in range(bins):
            group_mask = binned_series == bin_val
            if group_mask.sum() == 0:
                continue
            group_pos = (group_mask & pos_mask).sum()
            group_neg = (group_mask & neg_mask).sum()
            group_all = group_mask.sum()

            pos_cov = group_pos / total_pos if total_pos > 0 else 0
            neg_cov = group_neg / total_neg if total_neg > 0 else 0
            ratio = pos_cov / neg_cov if neg_cov > 0 else np.inf
            sample_pos_rate = group_pos / group_all if group_all > 0 else 0

            results['value_stats'].append({
                'value': f'bin_{int(bin_val)}',
                'count': int(group_all),
                'count_pos': int(group_pos),
                'count_neg': int(group_neg),
                'pos_coverage': float(pos_cov),
                'neg_coverage': float(neg_cov),
                'ratio': float(ratio) if ratio != np.inf else 'inf',
                'sample_pos_rate': float(sample_pos_rate)
            })

        iv_total = 0.0
        for stat in results['value_stats']:
            pos_cov = stat['pos_coverage']
            neg_cov = stat['neg_coverage']
            if pos_cov > 0 and neg_cov > 0:
                woe = np.log(pos_cov / neg_cov)
                iv = (pos_cov - neg_cov) * woe
                iv_total += iv
        results['IV'] = float(iv_total) if iv_total > 0 else 0.0

    except Exception:
        pass

    return results


print("=" * 60)
print("Step 3_2: 单变量初筛")
print("=" * 60)

print("\n[3_2_1] 从本地 workspace CSV 加载数据...")
train_df = pd.read_csv(
    OUTPUT_DIR / "step3_1_wide_table_train.csv",
    encoding="utf-8-sig",
    low_memory=False,
)
valid_df = pd.read_csv(
    OUTPUT_DIR / "step3_1_wide_table_valid.csv",
    encoding="utf-8-sig",
    low_memory=False,
)
print(f"  训练集: {len(train_df)} rows, {len(train_df.columns)} cols")
print(f"  验证集: {len(valid_df)} rows, {len(valid_df.columns)} cols")

exclude_cols = [USER_ID_COL, LABEL_COL, 'split']
feature_cols = [col for col in train_df.columns if col not in exclude_cols]
print(f"  待分析特征: {len(feature_cols)}")

total_pos = int((train_df[LABEL_COL] == 1).sum())
total_neg = int((train_df[LABEL_COL] == 0).sum())
print(f"  正样本: {total_pos}, 负样本: {total_neg}")

pos_mask_train = train_df[LABEL_COL] == 1
neg_mask_train = train_df[LABEL_COL] == 0

print("\n[3_2_2] 单变量分析（count_pos, count_neg, pos_coverage, neg_coverage, ratio, sample_pos_rate, IV）...")
feature_results = []
for i, col in enumerate(feature_cols):
    if (i + 1) % 50 == 0:
        print(f"  进度: {i + 1}/{len(feature_cols)}")

    try:
        series = train_df[col]
        n_unique = series.nunique()

        if n_unique <= 100:
            result = analyze_categorical_feature(series, pos_mask_train, neg_mask_train, total_pos, total_neg)
        else:
            result = analyze_numeric_feature(series, pos_mask_train, neg_mask_train, total_pos, total_neg)

        result['feature'] = col
        feature_results.append(result)
    except Exception as e:
        print(f"  警告: 分析特征 {col} 时出错: {e}")
        feature_results.append({'feature': col, 'n_unique': -1, 'missing_rate': 1.0, 'value_stats': [], 'IV': None})

print(f"  分析完成: {len(feature_results)} 个特征")

print("\n[3_2_3] 计算train/valid稳定性（PSI）...")
for r in feature_results:
    col = r['feature']
    try:
        psi = compute_psi(train_df[col], valid_df[col])
        r['PSI'] = psi
    except Exception:
        r['PSI'] = np.nan

print("\n[3_2_4] 生成建议并保存合并结果...")
merged_rows = []
for r in feature_results:
    rec = 'KEEP'
    reason = ''

    if r.get('missing_rate', 0) > 0.5:
        rec = 'DROP'
        reason = f"missing_rate={r['missing_rate']:.2%}>50%"
    elif r.get('n_unique', -1) == 1:
        rec = 'DROP'
        reason = 'constant feature'
    elif r.get('IV', 0) is not None and r['IV'] < 0.02:
        rec = 'CONSIDER_DROP'
        reason = f"IV={r['IV']:.4f}<0.02 weak predictive power"
    elif r.get('PSI', np.nan) is not None and not np.isnan(r['PSI']) and r['PSI'] > 0.2:
        rec = 'CONSIDER_DROP'
        reason = f"PSI={r['PSI']:.4f}>0.2 distribution unstable"

    merged_rows.append({
        'feature': r['feature'],
        'n_unique': r.get('n_unique', 0),
        'missing_rate': r.get('missing_rate', 0),
        'iv': r.get('IV'),
        'psi': r.get('PSI'),
        'recommendation': rec,
        'reason': reason
    })

merged_df = pd.DataFrame(merged_rows)
merged_df = merged_df.sort_values('iv', ascending=False, na_position='last')
merged_df.sort_values("feature").reset_index(drop=True).to_csv(
    OUTPUT_DIR / "step3_2_univariate_analysis.csv",
    index=False,
    encoding="utf-8-sig",
)

keep_count = (merged_df['recommendation'] == 'KEEP').sum()
consider_count = (merged_df['recommendation'] == 'CONSIDER_DROP').sum()
drop_count = (merged_df['recommendation'] == 'DROP').sum()
print(f"  KEEP: {keep_count}, CONSIDER_DROP: {consider_count}, DROP: {drop_count}")
print(f"  合并分析已写入本地 CSV: step3_2_univariate_analysis.csv ({len(merged_df)} rows)")

print("\n[3_2_5] 保存完整JSON...")
with open(OUTPUT_DIR / "step3_2_univariate_analysis_full.json", 'w', encoding='utf-8') as f:
    json.dump(feature_results, f, ensure_ascii=False, indent=2, default=str)
print(f"  完整结果已保存: step3_2_univariate_analysis_full.json")

print("\n  IV Top 10:")
top_iv = merged_df.dropna(subset=['iv']).head(10)
for _, row in top_iv.iterrows():
    psi_val = f"{row['psi']:.4f}" if not np.isnan(row['psi']) else 'N/A'
    print(f"    {row['feature']}: iv={row['iv']:.4f}, psi={psi_val}")

print("\n" + "=" * 60)
print("Step 3_2 完成!")
print("=" * 60)
