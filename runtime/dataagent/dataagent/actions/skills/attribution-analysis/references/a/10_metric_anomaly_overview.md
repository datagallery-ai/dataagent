# A 分支：指标异动归因 — 总览

本文件是指标异动归因的入口。目标是解释“指标为什么变了”“变化来自哪里”。这是归因分析中最常见的场景。

## 1. 适用范围

**适用于**：
- 指标异动解释（某指标突然上升/下降）
- 指标变化的贡献拆解（变化了多少来自 A，多少来自 B）
- 维度异常定位与候选根因定位（哪个渠道/地区/品类/设备组合最可疑）

**不适用于**：
- 功劳/信用分配（“各触点在路径上的功劳”→ 分支 B）
- 因果增量估计（“干预带来多少增量”→ 分支 C）

**硬性边界**：A 分支解释的是变化和统计集中，不直接给因果结论。

## 2. 常见输入与常见输出

### 常见输入

- actual vs expected
- before vs after
- forecast vs observed
- 维度表 / 多维属性表
- 基础指标与派生指标关系
- 指标定义、口径、时间范围、基准期说明

### 常见输出

- 贡献因子
- 候选维度组合
- 未解释残差 / 交互项 / 长尾部分
- 结论强度（统计集中 / 候选解释 / 候选根因）

## 3. 按指标公式类型路由

先判断目标 KPI 的公式结构，再决定主路径。A 分支里“公式怎么拆”和“维度定位在哪”是两件事。

| 指标结构 / 信号 | 主路径 | 何时叠加 |
|---------------|--------|---------|
| 可加总指标：总值由子项求和还原 | [11_additive_metrics.md](11_additive_metrics.md) | 需要找维度项 / 组合时叠加 [14_dimension_root_cause.md](14_dimension_root_cause.md) |
| 比率 / 商类派生指标：转化率、CTR、CVR、error rate、ARPU | [12_ratio_derived_metrics.md](12_ratio_derived_metrics.md) | 分子分母本身若可加总，可局部调用 [11_additive_metrics.md](11_additive_metrics.md)；需要 blame dimension 时叠加 [14_dimension_root_cause.md](14_dimension_root_cause.md) |
| 复杂派生 / 乘法或加乘链式 / 未知复杂计算 / 跨指标联合解释 | [13_nonlinear_composite_metrics.md](13_nonlinear_composite_metrics.md) | 需要定位维度组合时叠加 [14_dimension_root_cause.md](14_dimension_root_cause.md) |
| 目标是找哪个维度或维度组合最可疑（沿时间下钻） | 叠加 [14_dimension_root_cause.md](14_dimension_root_cause.md) | 14 是横向增强层，不替代 11 / 12 / 13 |
| 有 forecast、想快速缩小 blame scope、且更偏经典 Adtributor 设定 | 可参考 [15_adtributor.md](15_adtributor.md) | 15 更适合首轮 narrowing，不替代主路径分析 |
| **同一时点两个/多个共时群体的指标差异**（VIP vs 普通、北区 vs 南区） | [16_cross_section_group_comparison.md](16_cross_section_group_comparison.md) | **不要用 14 代替；群体对比与时序异动是不同问题** |
| 候选是**多个指标节点**（非维度切片）且节点间存在时序传播 / 依赖结构 | [17_multivariate_metric_propagation.md](17_multivariate_metric_propagation.md) | 与 14 互斥（候选对象不同）；与 15 不嵌套 |

### 多口径指标分流

| 问题口径 | 进入 |
|---------|------|
| 哪个品类 / 地区 / 商家贡献了 GMV 变化 | [11](11_additive_metrics.md) |
| GMV 变化来自流量、转化率、客单价哪个驱动 | [13](13_nonlinear_composite_metrics.md)，其中 CVR / AOV 等局部 ratio 调用 [12](12_ratio_derived_metrics.md) |
| X 群体 GMV / ARPU 为什么比 Y 群体高 | [16](16_cross_section_group_comparison.md) |

### 快速决策逻辑

1. 先问：同一群体随时间变化，还是同时点多群体差异？后者 → [16](16_cross_section_group_comparison.md)：**勿用 14 代替群体对比**，也勿套用「时序异动 → 14」叙事；但 **16 的第 3 节 Step 1** 仍须参照 [11](11_additive_metrics.md) / [12](12_ratio_derived_metrics.md) / [13](13_nonlinear_composite_metrics.md) 判别指标类型与分解（**不是**「完全不必打开 11/12/13」）
2. 时序问题，目标 KPI 能否由子项直接加和还原？能 → 11
3. 不能加和时，再问：它是否本质上是分子 / 分母或 success rate / quotient？是 → 12
4. 若既不是简单加和，也不是简单分子 / 分母，或需要乘法 / 加乘链式、多个相关指标联合解释 → 13
5. 只要问题变成"到底是哪几个维度值最可疑"（仍在时序下钻），就在主路径上叠加 14
6. 若有稳定 forecast、维度多、需先收缩排查范围，可用 15 做首轮 narrowing（结果仍回到 11 / 12 / 13，必要时叠加 14）
7. 若候选不是维度切片而是**多个指标节点**且节点间有传播 / 依赖结构 → [17](17_multivariate_metric_propagation.md)；与 14 互斥（候选对象不同），与 15 不嵌套

## 4. 默认流程

### Step 1. 明确目标指标与异常现象

**操作**：
- 确认指标名、单位、统计口径、时间范围
- 判断现象类别：上升 / 下降 / 波动异常 / 结构异常
- 确认是在解释“变化量”还是“当前水平差异”

**误区**：不区分“指标变化”和“指标绝对水平”。A 分支的分析对象首先是变化量。

### Step 2. 固定比较方式与基准

**操作**：
- 固定对比关系：actual vs forecast / 当期 vs 基期 / observed vs expected
- 检查两期口径可比性（详见 [02_data_preparation.md](../core/02_data_preparation.md)）
- 明确使用哪个基准做主结论，哪个基准做交叉核对

**终止条件**：如果两期不可比 → 停止量化分析，改为定性描述。

### Step 3. 进入 11 / 12 / 13 主路径

- 可加总 → 11
- 比率 / 派生 → 12
- 复杂派生 / 跨指标联合 → 13

### Step 4. 判断是否叠加 14 或 15

硬规则：**15 只用于 narrowing，不作为最终解释口径**。最终结论必须回到 11 / 12 / 13（必要时叠加 14）。

| 条件 | 操作 |
|------|------|
| 任务重点是“找哪个维度 / 维度项 / 维度组合最异常” | 叠加 [14_dimension_root_cause.md](14_dimension_root_cause.md) |
| 候选对象之间存在**因果传播 / 依赖关系**（前驱→后继结构） | 必须叠加 [14 的 Step 1.5](14_dimension_root_cause.md) 过滤层；不得仅按异常幅度 / 相关系数排根因 |
| 候选是指标节点而非维度切片（适用条件见第 3 节路由表） | 转 [17_multivariate_metric_propagation.md](17_multivariate_metric_propagation.md)；不与 14 / 15 嵌套 |
| 维度很多、且有 forecast、需要快速 narrowing | 可先跑 [15_adtributor.md](15_adtributor.md) |
| 只需公式层面拆解，不关心维度 | 不叠加 |

**强调**：14 是叠加层，不是 11 / 12 / 13 的替代品。15 是专项加速器，不是总入口。

### Step 5. 控制下钻深度与方法升级

- 先做单维和低阶组合 baseline
- 有明显头部时，优先报告头部和对冲项
- 没有明显头部、但维度空间大时，再考虑启发式搜索或 scope narrowing
- 下钻通常不超过 2 到 3 层；越深越容易变成长尾噪声解释

### Step 6. 给出结论强度与未解释部分

| 拥有的证据 | 结论强度 | 措辞 |
|----------|---------|------|
| 只有贡献拆解 | 统计集中 | “异常集中在 X” |
| 拆解 + 方向合理 | 候选解释 | “X 是主要候选解释之一” |
| 拆解 + 机制 / 事件 / 变更证据 | 候选根因 | “结合 XX 变更，X 为候选根因” |

**硬性规则**：
- 必须报告未解释残差 / 长尾 / 交互项
- 必须报告对冲项，不能只报同向拖累项
- 没有额外证据时，不使用确定性措辞

## 5. A 分支的常见边界问题

| 场景 | 正确进入 |
|------|---------|
| page view、revenue、error count、traffic、订单量 | 11 |
| conversion rate、CTR、CVR、error rate、ARPU | 12 |
| ROI、复杂评分、多个基础指标共同决定的 KPI | 13 |
| 想找哪个渠道、地区、设备组合最可疑 | 主路径 + 14 |
| 维度很多且有 forecast，想先快速缩小 blame scope | 主路径 + 15 |

## 6. 常见误区速查

| 误区 | 为什么错 | 正确做法 |
|------|---------|---------|
| 把转化率当加法指标拆 | sum(rate_i) 一般不能还原总体 rate | 进 12 |
| 只盯异常 KPI 本身，不回看基础指标 | 派生指标可能只是表象 | 比率进 12，复杂组合进 13 |
| 把 14 当成独立主路径 | 14 只解决“在哪个维度组合”，不解决“公式怎么拆” | 先定 11 / 12 / 13，再叠加 14 |
| 把 15 当成最终根因方法 | 15 更适合首轮缩小排查范围 | 需要时再升级到 14 或 13 |
| 在高相关维度间重复解释 | 不同维度可能只是同一现象的不同投影 | 各维度独立报告，并标注重叠 |
| 把贡献拆解直接写成因果结论 | A 分支不是因果识别 | 使用保守表达 |

## 7. 输出收口

A 分支按调用方契约输出；结构化输出边界见 [52_output_guardrails.md](../core/52_output_guardrails.md)。

禁止伪造 C 分支的因果字段。A 分支主结论限于统计集中、候选解释或候选根因，不输出增量效应。
