---
name: assign-researcher-for-neutralization-experiment
description: "当用户需要把某位研究员（用户）安排到某条实验记录上时使用。"
---

# SQLite Neutralization Assign Experiment Researcher

## Overview
1. 查找所有**操作/执行研究员**，任意选出一名`state `为空闲的**操作/执行研究员**；
2. 执行脚本`scripts/assign_experiment_researcher.py`指定**操作/执行研究员**操作实验。

## Required Inputs
- `experiment_id`（int：`experiments.id`）
- `researcher_user_id`（int：`users.id`，须已存在）

脚本调用：
```shell
python scripts/assign_experiment_researcher.py \
  --experiment_id <int> \
  --researcher_user_id <int>
```
