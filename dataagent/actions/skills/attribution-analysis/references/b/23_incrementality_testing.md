# B3. 增量测试与校准（B 校准层 / C 识别入口）

## 1. 职责定位

增量测试使用 C 分支的 RCT / 准实验识别逻辑（营销里最常见），但放在 B 目录中的职责是为 B 分支功劳归因（尤其 21 MTA/DDA 与 22 MMM）提供增量标尺与校准信号。**进入 23 不等于已经完成完整 C 分支**；只有报告要给出因果增量结论时，才继续按 [30](../c/30_causal_attribution_overview.md) 固定因果问句、estimand 与识别策略。

- 它回答"这次投放带来了多少真实增量（lift / iROAS）"
- 它常用于校准 B0/B1/B2，尤其是 21（MTA）与 22（MMM）；因果表述边界按 [20](20_contribution_attribution_overview.md) 第五步与 C 分支收口

**升级触发**：B 结论一旦用于"加预算 / 砍预算 / iROAS 报告 / 战略预算决策 / 声称增量价值"，必须至少经过 23 的增量校准；若要发布因果增量结论，继续接 [30](../c/30_causal_attribution_overview.md) → 31/32。详见 [01 的 5a](../core/01_problem_classification.md)。

**定位边界**：实验结果通常是某渠道、某时期、某市场条件下的**局部因果估计**，不能无条件外推；外推时应降低结论强度。

## 2. 何时优先做增量测试（路由规则）

| 场景 | 优先方法 | 原因 |
|------|---------|------|
| 大额预算重分配、需要因果标尺 | Geo experiment 或 Conversion Lift | 先拿因果标尺再做预算决策 |
| 用户级随机化不可行（TV/OOH/品牌广告） | Geo experiment / budget split | 通过地域或预算维度实现可比对照 |
| 平台支持 holdout 且渠道可寻址 | Conversion Lift / holdout | 实施成本低、解释直接 |
| 希望低干扰测增量 | Asymmetric budget split | 对投放流程改动较小 |

## 3. 设计前清单（必须明确）

1. 处理单元（user / geo / campaign / budget bucket）。
2. treatment 与 control 的定义与执行可行性。
3. response metric（sales / conversions / revenue）与归因窗口。
4. 实验目标：绝对 lift、相对 lift、iROAS，还是给 B0/B1/B2 提供 calibration signal。
5. spillover/interference 风险（跨 geo 外溢、受众串扰、跨渠道污染）。
6. 最小可检出效应与样本量/时长是否可支持。

## 4. 方法卡（按可实施性选择）

| 方法 | 适用数据粒度 | 输入要求 | 关键假设 | 何时优先用 | 何时不要用 | 输出是什么 | 与 B 分支如何衔接 |
|------|------------|---------|---------|-----------|-----------|-----------|------------------|
| Geo experiments | geo × time | geo 单元、预处理 KPI、treatment/control 方案、实验窗口 | 单元间干扰可控；处理组与对照组可比 | 用户级实验不可行，渠道、资源或策略可在地理维度干预 | geo 太少、spillover 严重且不可纠正 | lift、incremental sales、iROAS（含区间） | 作为 20a/22 的校准约束；限制 21 的预算解释力度 |
| Conversion Lift / holdout（含 Ghost Ads 可借鉴思路） | user/cookie/platform unit | 平台随机化能力、曝光/转化日志、实验分组定义 | 随机化有效，组间可比 | 数字可寻址渠道、触点或功能，需快速得到增量证据 | 平台不支持随机化或样本不足 | 绝对/相对 lift、iROAS（含区间） | 可用于 20a/21 定标与 22 先验更新 |
| Asymmetric budget split | campaign/market/resource 预算单元 | 预算切分方案、执行记录、结果指标 | 预算差异能形成可识别增量信号 | 需要低干扰、不中断常规投放或资源分配 | 执行不稳定、预算策略频繁变动 | 增量回报曲线、iROAS 区间 | 作为 20a/22 优化约束与 21 行动阈值参考 |

## 5. 默认执行流程（可直接落地）

### Step 1. 先定目标
- 先明确本次是测绝对 lift、相对 lift、iROAS，还是为 B0/B1/B2 提供 calibration signal。

### Step 2. 固定实验设计
- 明确 unit、treatment、control、response metric、实验窗口。
- 在执行前记录 spillover 风险与缓解方案。

### Step 3. 估计与诊断
- 给出点估计 + 区间估计 + 显著性/稳健性检查。
- 失败条件（功效不足、执行偏离、污染严重）要单列，不能藏在脚注。

### Step 4. 收敛结果
- 输出 lift/iROAS + 区间。
- 给出可推广性说明（适用渠道、适用时期、适用市场条件）。

### Step 5. 回流到 B 分支功劳归因
- 给出如何校准 20a/21/22 的明确建议，不停留在"实验做完"。

## 6. 校准 B 分支的接口规则

### 6.0 与 B0（通用功劳分配）的接口
- 将实验结果作为因果锚点，约束资源、功能、流程节点等功劳分配的解释边界。
- 不建议把总增量按 B0 份额机械摊到每个因素后写成细粒度因果贡献。

### 6.1 与 21（MTA）的接口
- 将实验结果作为因果锚点，约束 MTA 解释边界。
- 可在同渠道同窗口下比较尺度：

```
calibration_ratio = incrementality_estimate / attribution_estimate
```

- 若 ratio 明显偏离 1，优先解释口径与选择偏差，不建议机械一刀切缩放全部触点 credit。

### 6.2 与 22（MMM）的接口
- experiment 结果可作为 priors、模型筛选标准或后验约束。
- 关键说明必须写清：MMM 往往估计长期平均效果；实验常测短期/局部效果，二者不完全同物。
- 校准报告需显式声明"短期 vs 长期、局部 vs 整体"的映射假设。

## 7. Guardrails

结构化输出边界见 [52_output_guardrails.md](../core/52_output_guardrails.md)。本节只补增量测试的专项风险：

1. 实验估计常是局部效应，不应无脑外推到所有渠道、季度和地区。
2. spillover 与干预污染会直接破坏因果识别，需要单独报告风险等级。
3. 功效不足时应输出"暂不下结论"，而不是给确定性预算建议。
4. 23 的核心价值是给 B 分支功劳归因提供因果标尺；不回流校准则价值显著下降。
