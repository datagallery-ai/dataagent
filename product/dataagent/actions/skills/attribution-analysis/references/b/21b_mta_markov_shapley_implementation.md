# B1b. Markov / Shapley / Probabilistic MTA 实现细则

本文件服务于 [21_mta_multi_touch.md](21_mta_multi_touch.md) 的 `## 5. 方法二：Data-driven sequence-only`。只有在需要写代码实现 Markov removal effect、Shapley credit 或 Logistic/Probabilistic 路径模型时进入本文件。

## 1. 共同实现口径

方法二默认使用 path-level 数据，一条路径至少包含：有序触点列表、是否转化、可选转化价值。实现前必须固定：

1. `players`：参与归因的实体集合，默认是所有 channel。
2. `path_value`：转化路径的价值，默认转化数为 1；有收入口径时使用收入或净收入。
3. `converted=false` 的路径如何表示：Markov 默认终点为 `null/exit`；probabilistic 默认 target 为 0。
4. 重复触点处理：Markov 默认保留重复触点；Shapley 默认转成渠道集合（presence），除非明确做触点级 Shapley。
5. 标准化：所有方法都先输出 raw effect/credit，再输出标准化份额；不得只报百分比。

推荐输入至少包含：

```text
path_id, touchpoint, touch_order, converted, value(optional)
```

推荐输出至少包含：

```text
method, entity_type, entity_id, raw_credit, normalized_credit, value_scope, assumptions, warnings
```

## 2. Markov + Removal Effect

### 实现步骤

1. 预处理路径：每条路径转成 `start -> touchpoint_1 -> ... -> touchpoint_n -> conversion/null`；已转化路径终点为 `conversion`，未转化路径终点为 `null` 或 `exit`。
2. 统计相邻状态转移次数：`count[from_state, to_state] += 1`。
3. 行归一得到转移矩阵：`P[from_state, to_state] = count[from_state, to_state] / sum(count[from_state, *])`。
4. 计算基准转化概率 `p_all = Pr(start eventually reaches conversion)`。可用吸收马尔可夫链求解：对 transient states 构造 `Q`，对 `conversion` 构造 `R_conv`，则 `p_all = [(I - Q)^(-1) R_conv][start]`；也可用等价线性方程或迭代法。
5. 对每个 channel 做 removal：从状态空间中移除该 channel 及相关边，对剩余非吸收状态重新归一；若某行移除后无可用转移，默认转到 `null`，并记录 warning。
6. 重算 `p_without_channel`，计算：

```text
removal_effect(channel) = 1 - p_without_channel / p_all
```

7. 输出 `raw_credit = removal_effect`；若要份额，输出 `normalized_credit = raw_credit / sum(raw_credit)`，并同时保留 raw effect。

### 实现保护

- 若 `p_all <= 0`，停止并报告无法计算 removal effect。
- Markov 的转移矩阵每个非吸收状态行和应为 1；`conversion` 与 `null/exit` 为吸收状态。
- removal effect 可能为负，表示移除该 channel 后模型转化概率上升；不得静默截断为 0。若业务只接受非负份额，必须另报截断规则。
- removal effects 原始值通常不自然加总为 100%；只有 `normalized_credit` 可解释为份额。
- 只有转化路径、缺少 `null/exit` 时，不要输出高强度结论；代码应标记 `missing_non_conversion_paths=true`。

## 3. Shapley Credit

Shapley 必须先定义 value function `v(S)`：给定一组 players `S`，返回该组合的价值。价值可以是转化率、转化数、收入、净收入或模型预测值，但必须在输出中写明。

精确 Shapley 公式：

```text
phi_i = sum over S subset (N - {i}) of
        |S|! * (n - |S| - 1)! / n! * (v(S union {i}) - v(S))
```

### 实现步骤

1. 将路径映射到 players。默认做 channel-level Shapley：每条路径转成去重后的 channel 集合；重复触点不增加同一 channel 的玩家数量。
2. 固定 `v(S)`。若用经验 value，需说明是 exact-coalition、subset-based 还是模型估计；稀疏 coalition 不得硬算精确排序。
3. 当 players 数量较少（默认 `n <= 10`）时可枚举所有子集计算精确 Shapley。
4. 当 players 数量较多时使用 permutation sampling：随机排列 players，多次累加每个 player 加入当前 coalition 时的边际贡献；输出采样次数与稳定性检查。
5. 输出 `raw_credit = phi_i`；若要份额，输出 `normalized_credit = phi_i / sum(phi_i)`，并保留 `v(all_players) - v(empty_set)`。

### 实现保护

- 未定义 `v(S)` 时不得实现 Shapley；应先要求用户固定价值口径，或在代码中要求传入 `value_fn`。
- 精确 Shapley 应检查 `sum(phi_i) == v(all_players) - v(empty_set)`（允许数值误差）。
- 近似 Shapley 必须报告采样次数或误差检查。
- coalition 样本过稀时，优先合并低频渠道、提高聚合层级、或改用模型式 `value_fn`；不得伪造未观测组合的精确价值。
- Shapley 是公平分配规则，不自动代表因果增量。

## 4. Logistic / Probabilistic 路径模型

默认实现路径：

1. 构造 path-level 特征：channel presence、channel count、路径长度、首触/末触标记、可选顺序 n-gram；target 为 `converted`，可选样本权重为 `value`。
2. 划分训练/验证集；样本量小或共线性强时优先使用正则化 logistic regression / calibrated classifier。
3. 用验证集报告 AUC、log loss 或 calibration；模型未通过基本预测诊断时，不输出稳定 credit。
4. 若要 channel credit，使用 ablation：比较完整特征预测值与移除某 channel 相关特征后的预测值差异。
5. 输出平均预测差异作为 `raw_credit`，再按需要标准化。

实现保护：
- 系数大小不等于 credit；不要直接把 logistic coefficient 当贡献份额。
- ablation credit 受特征相关性影响，仍是模型依赖估计，不是因果效应。
