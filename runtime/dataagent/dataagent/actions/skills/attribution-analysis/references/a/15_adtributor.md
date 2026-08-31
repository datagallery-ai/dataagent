# A.5 Adtributor

## 1. 解决什么问题

Adtributor 更适合作为一个经典专项方法页来用：

- 有 forecast / expected 值
- 指标可能是 derived measures
- 需要快速定位 blame dimension
- 想先做 scope narrowing，而不是一步到位找最终根因

它的职责是：在多维空间里先把“最值得排查的维度和维度值”缩小出来。

## 2. 更适合什么场景

| 场景 | 是否适合 |
|------|---------|
| 有 actual 和 expected / forecast | 适合 |
| 维度多、人工逐个排查成本高 | 适合 |
| 指标是派生指标，需要先快速找 blame dimension | 适合 |
| 想做首轮 scope narrowing | 适合 |
| 没有 forecast / expected | 不适合 |
| 根因明显是复杂多维组合 | 升级到 14 |
| 需要跨指标复杂关系联合解释 | 升级到 13 |

## 3. 在整体 workflow 中的位置

```
指标异动发现 → 主路径公式拆解(11/12/13) → 维度候选定位(14)
                                           ↘ 需要快速 narrowing 时可先用 15
```

Adtributor 不是总入口，也不是最终根因分析器。它更像第一轮 blame scope 缩小器。

## 4. 输入要求

- actual 与 expected / forecast
- 至少一个可切片维度，通常是多个维度
- 若目标是 derived measure，需同时给出其 fundamental measures
- 维度明细应能回到总体

## 5. 默认流程

### Step 1. 准备 actual / expected 分布

对每个维度 D 的每个值 v，构建：

```
actual_v
expected_v
```

expected / forecast 缺失 → 停止本文件流程。

### Step 2. 先判断是基础指标还是派生指标

- **基础指标**：可直接做维度筛查
- **派生指标**：先拆到底层 fundamental measures，再判断 blame 主要来自哪一侧

- 对 ratio / derived measures，可把本文件作为 [12_ratio_derived_metrics.md](12_ratio_derived_metrics.md) 的快速首轮方法
- 根因不局限于单个维度或单个指标 → 升级到 14 或 13

### Step 3. EP / Surprise / Succinctness（Bhagwan et al., USENIX NSDI 2014）

派生指标见原文第 4 节。**下式仅适用于 fundamental measures**；派生 KPI 须在 **Step 2** 已落到可加总的 fundamental 层后再套用。全体在基本量下为各元素可加总（原文第 3.1 节）。

**记号**：维度 `i`、元素 `j`、测度 `m` 的预测与观测为 `F_ij(m)`、`A_ij(m)`；全体为 `F(m)`、`A(m)`。

**（1）Explanatory power（EP，原文式 (4)）**

```
EP_ij = ( A_ij(m) - F_ij(m) ) / ( A(m) - F(m) )
```

含义：该元素变化占总体变化的比例；单元素可大于 1 或小于 0；**同一维度内所有元素 EP 之和为 1**（原文第 3.2 节）。

**（2）Surprise（元素级，原文式 (5)–(7)）**

先定义份额 `p_ij = F_ij(m) / F(m)`，`q_ij = A_ij(m) / A(m)`。再按原文式 (7)（`log` 底与实现/原文一致）：

```
S_ij(m) = 0.5 * ( p_ij * log(2*p_ij / (p_ij + q_ij)) + q_ij * log(2*q_ij / (p_ij + q_ij)) )
```

`0 <= S_ij <= 1`。维度内按 `S_ij` 排序，用 `TEEP`、`TEP` 贪心组集（原文 Fig. 2）。

**（3）Succinctness**：候选元素集合的**元素个数**（越小越简洁）；在 EP 达标的多候选间作 tie-break（原文第 2 节、Fig. 2）。

### Step 4. 用 Surprise 排维度

`S_ij` 定义见 Step 3（式 (5)–(7)）。维度间排序与组集按原文 Fig. 2（`TEEP`、`TEP`）；勿用未声明的「相对偏差绝对值之和」等替代式，除非在报告中显式说明依据。

对每个维度计算实际分布与预期分布的偏离程度，先回答：
- 哪个维度最值得查
- 哪些维度暂时可以后放

这一步的作用是 blame dimension ranking，不是最终解释。

### Step 5. 用 Explanatory Power 排维度项

在头部维度内，对同向偏差的维度项排序，找出：
- 哪些元素解释了主要偏差
- 哪些元素只是噪声

### Step 6. 用 Succinctness 控制结果集

不要把所有偏差项都报出去，而要输出“最少解释集合”：
- 用尽可能少的项解释尽可能多的偏差
- 同时保留必要的对冲项说明

### Step 7. 输出缩小后的 blame scope

输出至少包含：
- 最可疑的维度
- 该维度中的头部元素
- 解释覆盖度
- 是否建议升级到 14 或 13

## 6. 衔接、局限与场景

- **→12**：可作 ratio / derived 的首轮 narrowing；最终仍须在 [12](12_ratio_derived_metrics.md) 写清分子、分母、结构/规模效应。
- **→14**：根因不限单维或需高阶组合时升级。
- **→13**：跨指标复杂链路时升级。

| 局限 | 处理 |
|------|------|
| 假设较强、仅适合 narrowing | 输出标为候选范围 |
| expected 质量 | 先检 forecast |
| 多维交叉 / 跨指标 | 升 14 或 13 |
| 非因果 | 保守措辞 |

| 场景 | 建议 |
|------|------|
| 有 forecast、维度多、需快速定位 blame dimension | 优先用 |
| 派生指标异常，需要先快速 scope narrowing | 优先用 |
| 维度只有 2 到 3 个，人工可直接看完 | 直接人工扫描 |
| 根因显然是复杂组合或跨指标链路 | 升级到 14 或 13 |
| expected 缺失或不可信 | 停止本文件流程 |

输出须标明 narrowing 性质与升级路径；结构化输出边界见 [52_output_guardrails.md](../core/52_output_guardrails.md)。
