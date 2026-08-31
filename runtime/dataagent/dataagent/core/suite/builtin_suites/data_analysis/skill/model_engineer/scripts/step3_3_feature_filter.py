"""
Step 3_3: 特征过滤
基于 step3_2 单变量筛选结果，删除 DROP 特征，保留 KEEP + CONSIDER_DROP
"""

import os
from pathlib import Path

import pandas as pd

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


print("=" * 60)
print("Step 3_3: 特征过滤")
print("=" * 60)

print("\n[3_3_1] 加载数据...")
train_df = pd.read_csv(OUTPUT_DIR / "step3_1_wide_table_train.csv", encoding='utf-8-sig', low_memory=False)
valid_df = pd.read_csv(OUTPUT_DIR / "step3_1_wide_table_valid.csv", encoding='utf-8-sig', low_memory=False)
univariate_df = pd.read_csv(OUTPUT_DIR / "step3_2_univariate_analysis.csv", encoding='utf-8-sig')
print(f"  训练集: {len(train_df)} rows, {len(train_df.columns)} cols")
print(f"  验证集: {len(valid_df)} rows, {len(valid_df.columns)} cols")
print(f"  step3_2特征: {len(univariate_df)} features")

print("\n[3_3_2] 筛选保留特征...")
drop_features = univariate_df[univariate_df['recommendation'] == 'DROP']['feature'].tolist()
keep_features = univariate_df[univariate_df['recommendation'] != 'DROP']['feature'].tolist()
print(f"  DROP: {len(drop_features)} features")
print(f"  KEEP + CONSIDER_DROP: {len(keep_features)} features")

print("\n[3_3_3] 删除DROP特征...")
keep_cols = [USER_ID_COL, LABEL_COL] + keep_features
train_filtered = train_df[keep_cols]
valid_filtered = valid_df[keep_cols]
print(f"  过滤后训练集: {len(train_filtered)} rows, {len(train_filtered.columns)} cols")
print(f"  过滤后验证集: {len(valid_filtered)} rows, {len(valid_filtered.columns)} cols")

print("\n[3_3_4] 保存结果...")
train_filtered.to_csv(OUTPUT_DIR / "step3_3_wide_table_train.csv", index=False, encoding='utf-8-sig')
valid_filtered.to_csv(OUTPUT_DIR / "step3_3_wide_table_valid.csv", index=False, encoding='utf-8-sig')

filtered_analysis = univariate_df[univariate_df['recommendation'] != 'DROP'].copy()
filtered_analysis.to_csv(OUTPUT_DIR / "step3_3_univariate_analysis.csv", index=False, encoding='utf-8-sig')

print(f"  已保存: step3_3_wide_table_train.csv")
print(f"  已保存: step3_3_wide_table_valid.csv")
print(f"  已保存: step3_3_univariate_analysis.csv ({len(filtered_analysis)} features)")

print(f"\n  保留特征: {len(keep_features)} (不含 {USER_ID_COL}/{LABEL_COL})")
print(f"  删除特征: {len(drop_features)}")

print("\n" + "=" * 60)
print("Step 3_3 完成!")
print("=" * 60)
