# A.2 比率 / 派生指标归因

## 1. 适用范围

指标是比值或“每 X 的 Y”，分子与分母同时变化。

**典型指标**：success rate、conversion rate、CTR、CVR、error rate、ARPU、CPA、CPC。

**不适用于**：
- 纯加总指标（→ [11_additive_metrics.md](11_additive_metrics.md)）
- 多因子非线性组合、跨指标联合解释（→ [13_nonlinear_composite_metrics.md](13_nonlinear_composite_metrics.md)）

## 2. 输入要求

- 目标 ratio / quotient 的显式公式
- 分子与分母的 actual / expected，或 before / after
- 若需做维度定位，需同时拿到分子和分母的维度切片
- 若 ratio 只是更大复合 KPI 的一部分，需明确其上游和下游关系

## 3. 为什么不能只盯着 ratio 本身

这是本文件存在的核心原因：

- 比率不是加和对象，通常不能直接按 rate 相加还原总体
- 分母决定权重，高流量或高曝光组的变化影响更大
- 分子与分母会同时变化，只看 ratio 容易误读
- 派生指标异常可能来自多个基础指标的小幅联动偏移，而不是 ratio 自己“突然坏掉”

因此，A 分支处理 ratio / derived measure 时，必须先追到底层 fundamental measures。

## 3.1 符号与最简公式（核对口径）

以下只收**可核对**、且与本文件流程直接相关的式子；实现细节仍以业务口径为准。

**（1）比率与两期对数恒等式**

记 `R = N/D`（如转化率 = 转化数 / 曝光），下标 `0`、`1` 表示两期或两基准。恒等式：

```
ln(R1/R0) = ln(N1/N0) - ln(D1/D0)
```

含义：比率的对数变化 = 分子对数变化 − 分母对数变化。用于先把「商」变成「对数域上的加减」，再与因子贡献叙述对齐（与乘积型指标上常用的对数分解思路一致）。

**（2）分层总体下「率差」的 Kitagawa 对称分解（结构 / 率效应）**

当总体率可写成互斥分层上的加权和 `R = sum_k (w_k * r_k)`（`w_k` 为分层权重或占比，`r_k` 为分层内率），两总体（或两时点结构对比）`A` 与 `B` 的 crude rate 之差满足 **Kitagawa (1955)** 的常见对称写法：

```
R^A - R^B = sum_k w_bar_k * (r_k^A - r_k^B) + sum_k r_bar_k * (w_k^A - w_k^B)

w_bar_k = (w_k^A + w_k^B) / 2
r_bar_k = (r_k^A + r_k^B) / 2
```

第一项常解释为「组内率差异（rate / 质量）」、第二项为「权重 / 构成差异（composition / 结构）」；与第 4 节中「结构 / 规模 / 分子分母」类拆解叙述同族。出处：Kitagawa (1955), *JASA* 50(272):1168–1194.

**注意**：这是**统计分解**，不自动等于因果效应；与 [16](16_cross_section_group_comparison.md) 的群体差分解可对照使用，但问题设定不同。

## 4. 默认流程

### Step 1. 先拆分分子和分母

把目标指标写成显式公式：
- 转化率 = 转化数 / 访问数
- CTR = 点击数 / 曝光数
- error rate = 错误数 / 请求数
- ARPU = 收入 / 活跃用户数

先输出一张基础审计表：
- 分子变化
- 分母变化
- ratio 变化
- 分子和分母的变化方向是否一致

### Step 2. 判断异常主要来自 numerator、denominator，还是两者共同变化

| 情况 | 初步判断 | 下一步 |
|------|---------|------|
| 分子主导 | 先排查分子 | 若分子可加总，局部调用 [11_additive_metrics.md](11_additive_metrics.md) |
| 分母主导 | 先排查分母 | 若分母可加总，局部调用 [11_additive_metrics.md](11_additive_metrics.md) |
| 两者共同变化 | 不直接下结论 | 继续做结构效应 / 规模效应拆解 |
| 比率只是复杂 KPI 的局部一环 | ratio 不是终点 | 转 [13_nonlinear_composite_metrics.md](13_nonlinear_composite_metrics.md) |

### Step 3. 再决定是否进入 derived-measure root cause localization

当 ratio 异常不能由“分子单独变了”或“分母单独变了”解释时，要继续区分：
- **numerator effect**：分子本身变化带来的 effect
- **denominator effect**：分母变化带来的 effect
- **structure effect**：组间权重迁移导致的 effect
- **scale effect**：总体规模变化带来的 effect
- **interaction / residual**：无法被前几项充分解释的部分

**硬性规则**：最终输出里必须显式写出“分母效应 / 结构效应 / 规模效应”，避免把 rate 变化误写成“质量变化”。

### Step 4. 选择分析路线

| 路线 | 适用条件 | 方法 | 与其他路径的关系 |
|------|---------|------|----------------|
| 简单双因子 ratio | 分子 / 分母清晰，且无明显多指标联动 | 有限差分 / 替换法 | 分子分母可加总时，局部调用 11 |
| 分组 ratio | 需要区分组内变化与组间结构迁移 | mix-rate decomposition | 需要定位维度时叠加 14 |
| derived measure root cause localization | 派生指标背后有多个基础指标或多项小幅偏移 | 先回看 fundamentals，再做联合解释 | 若超出简单 ratio，转 13 |

### Step 5. 维度定位时的处理

- 若分子和分母都是可加总指标，分别对它们运行 [11_additive_metrics.md](11_additive_metrics.md)
- 若目标是“哪几个维度值造成了 ratio 异常”，在分子和分母层面叠加 [14_dimension_root_cause.md](14_dimension_root_cause.md)
- 若维度多且有 forecast，可用 [15_adtributor.md](15_adtributor.md) 做首轮 blame dimension 缩小

## 5. 与 11 和 13 的边界

### 与 11 的边界

- 分子和分母本身若是加法指标，可局部调用 [11_additive_metrics.md](11_additive_metrics.md)
- 但不能把 ratio 直接当作加法指标处理

### 与 13 的边界

- 如果 ratio 只是更复杂复合 KPI 的一部分，或多个基础指标关系已不再是简单分子 / 分母 → 转 [13_nonlinear_composite_metrics.md](13_nonlinear_composite_metrics.md)
- 如果 residual / interaction 已经大到不能接受，也应转 13

## 6. 何时优先用 / 何时不要用

| 场景 | 建议 |
|------|------|
| success rate、conversion rate、CTR、CVR、error rate 这类标准比率 | 优先使用本文件 |
| ratio 的分子分母清晰、且可单独审计 | 优先使用本文件 |
| ratio 只是复杂业务指数的一部分 | 不要停留在本文件，转 13 |
| 只有 ratio 汇总值，没有分子分母明细 | 只能做弱解释，不做强归因 |
| 分母极小、极不稳定 | 谨慎，必要时合并组或降级为定性 |

## 7. 常见误区

| 误用 | 问题 | 正确做法 |
|------|------|---------|
| 按维度项直接加总 rate | sum(rate_i) 一般不能还原 total_rate | 用分子分母和权重重建 |
| ratio 降了就直接说“质量下降” | 可能只是分母或结构变化 | 显式报告分母效应 / 结构效应 |
| 只看 ratio，不看基础指标 | 容易漏掉真正的 fundamental change | 先审计分子和分母 |
| 把派生指标异常完全归因给单一组 | 可能是多项小偏移叠加 | 继续追 fundamentals 或升级 13 |
| 用 rate 变化排序维度 | 小分母组噪声放大 | 用加权贡献或结构拆解 |

输出边界与保守措辞见 [52_output_guardrails.md](../core/52_output_guardrails.md)；须显式区分 numerator / denominator / 结构或规模效应，并声明非因果。
