# B1c. Timestamp-Aware MTA 实现细则

本文件服务于 [21_mta_multi_touch.md](21_mta_multi_touch.md) 的 `## 6. 方法三：Timestamp-aware`。只有在需要写代码实现 Time-to-event / Survival MTA 或 Poisson / Point-process MTA 时进入本文件。

## 1. 进入条件

Timestamp-aware 方法适用于有可靠时间戳、需要刻画触点时滞或转化前未转化删失的场景。

进入前必须确认：

1. 触点时间、转化时间、观察窗口可信。
2. 未转化用户或未转化路径可作为删失样本保留。
3. 时间粒度稳定，曝光/点击/转化日志没有明显系统性漏记。
4. 业务接受输出为模型依赖估计，而不是实验因果增量。

规则 baseline 中的 time-decay 见 [21a_mta_baseline_implementation.md](21a_mta_baseline_implementation.md)，不属于本文件。

## 2. Time-to-event / Survival MTA

默认实现：

1. 将用户路径展开为 user-time 或 start-stop 区间数据：`user_id, interval_start, interval_end, event, censor_flag, time_varying_touch_features`。
2. 未转化用户必须作为右删失样本保留；缺少未转化样本时不要实现 survival MTA。
3. 选择模型：Cox time-varying、离散时间 hazard（logistic / complementary log-log）或 AFT；选择哪一种必须写在参数中。
4. 输出 channel 的时变影响、风险比或预测转化概率变化；若要 credit，使用预测风险 ablation，而不是直接把 hazard ratio 当份额。
5. 报告删失窗口、时间粒度、是否存在延迟转化与左截断风险。

输出保护：
- 无可靠时间戳时，代码必须拒绝 survival MTA，或降级到 [21b](21b_mta_markov_shapley_implementation.md) 的 sequence-only 方法。
- 未转化样本缺失时，不得把 survival 输出写成稳定 credit。
- Hazard ratio 不是 credit 份额；若要份额，必须通过预测风险 ablation 后再标准化。

## 3. Poisson / Point-Process MTA

默认实现：

1. 将时间切成稳定粒度的 bin，或使用事件流格式；每个 bin 包含曝光/触点强度、历史触点衰减特征、是否发生转化。
2. 建模转化强度 `lambda(t)`，常见可实现形式是 Poisson regression / GLM；高阶 Hawkes 或点过程只在事件量足够且时间戳可靠时使用。
3. 对每个 channel 做强度 ablation：比较完整强度与移除该 channel 强度后的预期转化数差异。
4. 输出 `raw_credit = expected_conversions_full - expected_conversions_without_channel`，再按需要标准化。

输出保护：
- 时间戳粒度不稳定、曝光漏记、转化时间延迟严重时，不要输出精确 credit。
- 高频触点事件不足时，不要上高阶 point-process；优先用离散时间 hazard 或 [21b](21b_mta_markov_shapley_implementation.md) 的 sequence-only 方法。
- time-aware 方法输出的通常是模型依赖的时变关联贡献；预算动作仍需 [23_incrementality_testing.md](23_incrementality_testing.md) 的增量校准。

## 4. 推荐输出

```text
method, entity_type, entity_id, raw_credit, normalized_credit, time_window, model_type, assumptions, warnings
```

输出时必须同时报告：
- 时间窗口与时间粒度。
- 删失处理方式。
- 是否使用 ablation 生成 credit。
- 模型诊断或失败条件。
