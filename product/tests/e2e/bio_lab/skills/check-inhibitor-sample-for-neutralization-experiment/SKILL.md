---
name: check-inhibitor-sample-for-neutralization-experiment
description: "当用户需要检查某个样本在中和实验中是否满足最低余量和浓度要求时使用。"
---

# SQLite Neutralization Check Sample

## Overview
该技能检查指定 `sample_id` 的 `体积`（余量）和 `浓度` 是否满足阈值。如果用户未提供，就使用如下的默认参数：样本的最小余量为200 ul，最小浓度为1

## Required Inputs
- `sample_id`（int，对应 `样本ID`）
- `min_remaining_volume`（number，单位为ul，对应 `体积` 的最小值，>=）
- `min_concentration`（number，对应 `浓度` 的最小值，>=）

## Outputs
输出判断结果（通过/不通过 + 对应的实际余量/浓度）

脚本调用：
```shell
python scripts/check_inhibitor_sample_sufficiency.py \
  --sample_id <int> \
  --min_remaining_volume <number> \
  --min_concentration <number>
```
