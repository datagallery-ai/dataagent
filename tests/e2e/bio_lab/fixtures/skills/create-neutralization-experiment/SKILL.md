---
name: create-neutralization-experiment
description: "当用户需要创建中和实验时使用。"
---

# SQLite Neutralization Create Experiment

## Workflow
1. 找到符合要求的`cell`，`inhibitor`和`pseudovirus`的 样本id，要求如下：
  - 三种样本都需要满足的要求（以`cell`为例）：
    - 如果用户提问中明确指定了`cell`，并且该`cell`只有一个样本，则无需询问用户。
    - 如果用户提问中明确指定了`cell`，并且该`cell`有大于一个样本，则必须调用`request_human_feedback`tool询问用户使用哪个样本ID。
    - 如果用户提问中没有提供`cell`的信息，则必须询问用户使用哪个`cell`。进一步地，如果用户反馈的`cell`有多个样本，必须调用`request_human_feedback`tool询问用户使用哪个`cell`样本。
  - 对于inhibitor样本，需要调用`check-inhibitor-sample-for-neutralization-experiment`判断样本是否满足实验要求；
  - 所有样本被确定之后，必须调用`request_human_feedback`tool咨询用户是否可以创建实验；

2. 执行脚本`scripts/insert_neutralization_experiment.py`完成实验创建；

3. 使用`assign-researcher-for-neutralization-experiment`skill为实验分配空闲研究员。

## Requirements
- 只需关注三个样本参数，其余参数无需填写。

## Reference
```shell
python scripts/insert_neutralization_experiment.py \
  --cell_sample_id <int> --inhibitor_sample_id <int> --pseudovirus_sample_id <int>
```
