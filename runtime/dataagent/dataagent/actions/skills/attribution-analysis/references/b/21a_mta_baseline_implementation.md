# B1a. MTA 规则 Baseline 实现细则

本文件服务于 [21_mta_multi_touch.md](21_mta_multi_touch.md) 的 `## 4. 方法一：规则 baseline`。只有在需要写代码实现 first-touch、last-touch、linear、position-based/U-shaped 或 time-decay 时进入本文件。

## 1. 输入与输出契约

默认只对已转化路径分配 credit；非转化路径可用于诊断覆盖率，但不参与规则 credit 分配。若业务要求用收入或净收入，令每条转化路径的待分配值为 `path_value`；否则 `path_value = 1`。

推荐输入至少包含：

```text
path_id, touchpoint, touch_order, touch_time(optional), converted, value(optional)
```

推荐输出至少包含：

```text
method, entity_type, entity_id, raw_credit, normalized_credit, value_scope, assumptions, warnings
```

## 2. 方法实现规则

| 方法 | 实现规则 |
|------|---------|
| First-touch | 每条转化路径将 `path_value` 全部分给第一个触点；聚合到目标粒度后求和 |
| Last-touch | 每条转化路径将 `path_value` 全部分给最后一个触点；聚合到目标粒度后求和 |
| Linear | 每条转化路径有 `n` 个触点时，每个触点分 `path_value / n`；重复触点默认重复计入 |
| Position-based / U-shaped | `n=1` 时该触点拿 100%；`n=2` 时首末各 50%；`n>=3` 时默认首触 40%、末触 40%、中间触点均分 20%；若改比例必须参数化 |
| Time-decay | 仅在有触点时间与转化时间时使用；默认用半衰期权重 `weight = 0.5 ** (time_to_conversion / half_life)`，再按权重归一后分配 `path_value` |

## 3. 输出与验收

- 输出每种 baseline 的 `raw_credit`、`normalized_credit = raw_credit / sum(raw_credit)`。
- baseline 方法的 `normalized_credit` 应能加总到 1（或 100%）；若按价值分配，`raw_credit` 应能加总到输入总转化价值。
- 同时输出路径数、转化路径数、平均路径长度、最大路径长度；路径极长或重复触点很多时提示结果可能受频次影响。
- 若实现去重版本，必须另设参数（如 `dedupe_within_path=true`），不得覆盖默认的保留重复触点版本。
- 无可靠时间戳时，代码必须拒绝 time-decay，或自动降级到不含 time-decay 的 baseline 集合并写入 warning。

## 4. 禁止的错误实现

1. 不得把 last-touch 默认当最终结论；所有规则 baseline 都只是对照。
2. 不得在没有触点时间和转化时间时实现 time-decay。
3. 不得把 U-shaped 的 40/20/40 写成数据发现；它是人为固定权重。
4. 不得只输出百分比而丢失 `raw_credit`。
