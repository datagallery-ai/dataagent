"""
Step 3_1: 确定y-label并生成训练集和验证集
- 确定 LABEL_COL（来自 schema_resolution）为 y-label
- 训练集: 80% 正样本 + 80% 负样本
- 验证集: 20% 正样本 + 20% 负样本
- 输入: OUTPUT_DIR/step2_5_wide_userfiltered.csv
- 输出: OUTPUT_DIR/step3_1_wide_table_train.csv, OUTPUT_DIR/step3_1_wide_table_valid.csv
"""

import json
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

RANDOM_STATE = 42

print("=" * 60)
print("Step 3_1: 确定y-label并生成训练集和验证集")
print("=" * 60)

input_path = OUTPUT_DIR / "step2_5_wide_userfiltered.csv"
print(f"\n[3_1_1] 从本地 CSV 加载 {input_path.name}...")
wide_df = pd.read_csv(input_path, encoding="utf-8-sig", low_memory=False)
print(f"  加载完成: {len(wide_df)} rows, {len(wide_df.columns)} cols")

print("\n[3_1_2] 确定y-label列...")
y_label_col = LABEL_COL
print(f"  y-label列: {y_label_col}")
print(f"  正样本(label=1): {(wide_df[y_label_col]==1).sum()}")
print(f"  负样本(label=0): {(wide_df[y_label_col]==0).sum()}")

print("\n[3_1_3] 划分正负样本...")
positive_df = wide_df[wide_df[y_label_col] == 1].copy()
negative_df = wide_df[wide_df[y_label_col] == 0].copy()
print(f"  正样本: {len(positive_df)} rows")
print(f"  负样本: {len(negative_df)} rows")

print("\n[3_1_4] 划分训练集和验证集...")
positive_df = positive_df.sample(frac=1, random_state=RANDOM_STATE).reset_index(drop=True)
negative_df = negative_df.sample(frac=1, random_state=RANDOM_STATE).reset_index(drop=True)

train_ratio = 0.8
pos_split_idx = int(len(positive_df) * train_ratio)
positive_train = positive_df.iloc[:pos_split_idx].copy()
positive_valid = positive_df.iloc[pos_split_idx:].copy()

neg_split_idx = int(len(negative_df) * train_ratio)
negative_train = negative_df.iloc[:neg_split_idx].copy()
negative_valid = negative_df.iloc[neg_split_idx:].copy()

train_df = pd.concat([positive_train, negative_train], ignore_index=True)
valid_df = pd.concat([positive_valid, negative_valid], ignore_index=True)

print(f"  正样本训练集: {len(positive_train)} rows")
print(f"  正样本验证集: {len(positive_valid)} rows")
print(f"  负样本训练集: {len(negative_train)} rows")
print(f"  负样本验证集: {len(negative_valid)} rows")
print(f"\n  最终训练集: {len(train_df)} rows")
print(f"  最终验证集: {len(valid_df)} rows")

train_usids = set(train_df[USER_ID_COL])
valid_usids = set(valid_df[USER_ID_COL])
overlap = train_usids & valid_usids
print(f"\n  训练集usid数: {len(train_usids)}")
print(f"  验证集usid数: {len(valid_usids)}")
print(f"  usid重叠数: {len(overlap)}")

print("\n[3_1_5] 写入本地 workspace CSV...")
train_df.to_csv(OUTPUT_DIR / "step3_1_wide_table_train.csv", index=False, encoding="utf-8-sig")
valid_df.to_csv(OUTPUT_DIR / "step3_1_wide_table_valid.csv", index=False, encoding="utf-8-sig")
print("  训练集已写入: step3_1_wide_table_train.csv")
print("  验证集已写入: step3_1_wide_table_valid.csv")

split_report = {
    "y_label_column": y_label_col,
    "split_method": "正负样本各80/20分层抽样",
    "random_state": RANDOM_STATE,
    "train_ratio": train_ratio,
    "total_users": len(wide_df),
    "train_size": len(train_df),
    "valid_size": len(valid_df),
    "positive_distribution": {
        "train_positive": int((train_df[LABEL_COL] == 1).sum()),
        "valid_positive": int((valid_df[LABEL_COL] == 1).sum()),
        "total_positive": int((wide_df[LABEL_COL] == 1).sum())
    },
    "negative_distribution": {
        "train_negative": int((train_df[LABEL_COL] == 0).sum()),
        "valid_negative": int((valid_df[LABEL_COL] == 0).sum()),
        "total_negative": int((wide_df[LABEL_COL] == 0).sum())
    }
}
with open(OUTPUT_DIR / "step3_1_split_report.json", 'w', encoding='utf-8') as f:
    json.dump(split_report, f, ensure_ascii=False, indent=2)
print("  划分报告已保存到 step3_1_split_report.json")

print("\n" + "=" * 60)
print("Step 3_1 完成!")
print("=" * 60)
