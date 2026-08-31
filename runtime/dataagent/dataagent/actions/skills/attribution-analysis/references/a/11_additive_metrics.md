# A.1 加法型指标异动归因

## 1. 适用问题

指标本质是求和，总变化可由子项变化加总还原。

**典型指标**：page view、revenue、error count、traffic、订单量、点击量、曝光量、成本总额。

**不适用于**：转化率、均价、ROI、CPC 等比率或派生指标。判别方式：各子项加起来是否等于总值。

## 2. 输入要求

- 目标 KPI 的 actual 与 expected，或 before 与 after
- 至少一个可切片的维度表，且维度值能覆盖总体
- 若做多维定位，需有多维属性空间，而不是只有单一汇总值
- 若使用 forecast 作为 expected，需先确认 forecast 质量可接受

## 3. 关键假设与边界

- **可加总**：总值必须能由底层单元加和还原
- **口径一致**：actual 和 expected，或 before 和 after 必须可比
- **覆盖完整**：被分析维度不能漏掉大块流量 / 收入 / 错误量
- **统计集中不是因果**：找到了异常子空间，不等于已经找到因果原因

## 4. 默认流程

### Step 1. 校验指标是否满足可加总

先做三件事：
- 总值能否由明细重新加和
- 维度切片相加后是否回到总体
- 基准与当前是否口径一致

任一不通过 → 不要进入加法拆解，回到 [10_metric_anomaly_overview.md](10_metric_anomaly_overview.md) 重新选路。

### Step 2. 固定 actual / expected 或 before / after

优先级：
1. 有稳定 forecast 时，用 actual vs expected
2. 无 forecast 时，用 before vs after
3. 若同时可得，主报告固定一个基准，另一个做 sanity check

输出：
- 总体变化量 Delta
- 总体变化率 Delta_pct
- 主基准说明

### Step 3. 先做单维和低阶组合 baseline

对每个候选维度 D，先计算：

```
delta_i = actual_i - expected_i
contribution_i = delta_i / Delta
```

先看：
- 单维头部项覆盖度
- 同向拖累项和反向对冲项
- 低阶组合是否已经足够解释异常

**标准 baseline**：
- 单维扫描
- 少量低阶组合，如 渠道 × 地区、设备 × 版本
- 不要一开始就跑全量高阶组合

### Step 4. 判断是否需要多维候选搜索

下列信号同时满足时，再从简单 contribution split 升级到 HotSpot 类思路：
- 单维解释不够，头部覆盖度低
- 怀疑异常集中在多维属性组合而非单一维度值
- 有 actual / expected 或可比较基准
- 维度空间大，人工枚举成本高

若单维头部已覆盖大部分变化，不必强行升级。

### Step 5. 做多维候选搜索

HotSpot 类流程：

1. **定义候选子空间**：候选不是单个维度值，而是 attribute-value combination
2. **从低阶开始**：先看单维，再看二阶，再决定是否进入更高阶
3. **利用 ripple effect / anomaly propagation**：若某个上层聚合异常明显，优先沿其下游明细继续下钻，而不是全空间盲搜
4. **对候选打分**：用 potential score 衡量该候选对子异常方向、异常幅度、覆盖度的综合解释能力
5. **做启发式搜索**：维度空间很大时，用分层剪枝、逐层扩展、优先队列或 MCTS 类启发式，而不是穷举所有组合

potential score 至少包含：
- 候选对子异常方向是否一致
- 候选对总体异常解释了多少
- 候选本身样本是否足够稳定
- 候选是否与已有头部候选高度重叠

### Step 6. 输出 top root-cause candidates 与对冲项

输出至少包含：
- top root-cause candidates
- 每个候选解释的异常幅度或覆盖度
- 反向对冲项
- 未解释残差 / 长尾部分

高重叠候选须合并或标注“可能是同一现象的不同投影”。

## 5. 什么时候 HotSpot 类思路比简单 contribution split 更值得用

| 场景 | 选择 |
|------|------|
| 单维头部很明显，Top-3 已解释大部分变化 | 简单 contribution split 即可 |
| 异常分散在多个低阶维度交叉上 | 优先考虑 HotSpot 类思路 |
| 指标是加法型，且有多维属性空间 | HotSpot 类思路收益较大 |
| 只有总量，没有维度明细 | 无法使用 |
| 只需要业务首轮结论，不需要深挖组合 | 先做简单 split |

## 6. 常见失败条件 / 注意事项

| 问题 | 影响 | 处理 |
|------|------|------|
| forecast 不稳 | actual vs expected 失真 | 先校验 forecast 误差，必要时改用 before vs after |
| 搜索空间过大 | 组合爆炸、噪声候选过多 | 先单维、再低阶、再启发式搜索 |
| 启发式方法 | 不保证全局最优 | 报告为候选集合，不报唯一真因 |
| 稀疏子空间太多 | 小样本噪声放大 | 设最小支持度阈值 |
| 同一现象多次投影 | 结果重复解释 | 合并高重叠候选并标注 |

## 7. 收口

- 叠加 14 / 15 或退出到其他主路径的条件见 [10](10_metric_anomaly_overview.md)。
- 输出边界见 [52_output_guardrails.md](../core/52_output_guardrails.md)。
- 必报头部、对冲项、未解释残差；声明非因果。
