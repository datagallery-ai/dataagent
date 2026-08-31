# B 分支：Contribution Attribution（功劳归因）— 总览

本文件是 **B 路径**入口。目标是回答「在一个**已发生结果**下，多个参与因素各自分多少功劳 / credit / contribution，以及这些结论能支持多大力度的决策」。

Marketing Attribution 是 B 的重要应用场景，其中 MTA、DDA、MMM 等任务本质上属于 B；但 Marketing Attribution 作为业务领域还会涉及变化分析、因果增量分析和预算优化，因此不能与 B 分支画等号。

B 路径默认遵循「先定义功劳口径 → 先快后准 → 必要时用增量证据校准」：先固定结果、候选贡献因素、分配规则和数据边界，再给可解释 baseline，必要时做数据驱动建模，最后用增量测试或 C 分支证据校准结论强度。

## 1. 与 A / C 分支的根本区别

总口径区分以 `SKILL.md` 为准；本节仅用于 B 分支防误入。

| 分支 | 核心问句 | 分析对象 | 输出 |
|------|---------|--------|------|
| A 指标异动 | "指标为什么变了 / 两组为什么不同" | 变化量、差异量 | 解释变化/差异的维度项或候选因素 |
| B 功劳归因 | "既定结果下谁贡献了多少" | 已发生结果及其参与因素集合 | 功劳份额、贡献金额/数量、贡献排序、可选资源/预算建议 |
| C 因果归因 | "如果没有这个因素/干预，结果会少多少" | treatment 与反事实结果 | 增量效应、置信区间、识别假设 |

**常见混淆**：
- "GMV 下降了，各渠道贡献多少"若是**两期按渠道切片拆解**，属于 **A**。只有明确是在某个既定 GMV / 转化结果下分配渠道 credit，才进入 **B**。
- "这个渠道带来了多少增量"属于 **C**，或 B + C 校准；不能只用 B 的 credit 直接写成因果增量。

## 2. B 路径子问题与进入条件

| 子方向 | 核心问题 | 典型数据粒度 | 常见输入 | 常见输出 | 何时进入 | 详细文件 |
|--------|--------|-------------|---------|---------|---------|---------|
| B0 通用功劳分配 | 资源、功能、团队、流程节点等对既定结果分多少功劳 | entity/component/item | 已发生结果、候选因素、参与证据、分配规则或模型特征 | contribution share、贡献金额/数量、排序 | 非营销或非路径场景，目标是信用分配而不是变化解释 | [20a_general_contribution_allocation.md](20a_general_contribution_allocation.md) |
| B1 MTA / DDA | 多触点路径功劳分配 | user/path/touchpoint | user path、touchpoint logs、conversion path | 触点/渠道 credit、路径洞察 | 有用户级触点序列，目标是路径级或战术层优化 | [21_mta_multi_touch.md](21_mta_multi_touch.md) |
| B2 MMM / 聚合贡献 | 聚合层渠道、资源或投入的贡献与边际回报 | weekly/monthly、geo/national | aggregated spend/input + KPI + controls | 渠道/资源贡献、ROAS/mROAS、预算或资源配比方案 | 只有聚合数据，目标是策略层配置 | [22_mmm_marketing_mix.md](22_mmm_marketing_mix.md) |
| B3 增量测试 | 提供增量标尺并校准 B 结论 | geo/campaign/user experiment | treatment-control 设置、实验窗口、response metric | lift、iROAS、incremental sales、校准信号 | 需要增量标尺、要校准 observational 结果，或要声称增量价值 | [23_incrementality_testing.md](23_incrementality_testing.md) |

**定位强调**：B3 经常是 B0/B1/B2 的校准层，不只是独立分支。高金额预算、资源分配或 ROI 决策默认应检查是否具备 B3 或 C 分支校准证据。若要形成因果结论，进入 C 分支收口。

### Marketing Attribution 与 Contribution Attribution 的关系

Marketing Attribution 是 B 的重要应用，但不是 B 的同义词：

- MTA / DDA：营销触点路径上的 credit allocation，属于 B1。
- MMM：聚合层营销投入对业务结果的贡献建模，属于 B2。
- Channel Attribution：渠道贡献或功劳分配，通常属于 B1 或 B2。
- Incrementality Testing：提供因果增量标尺，属于 B3 或 C；它校准 B，但不等同于 B。
- Marketing Driver Analysis：若解释的是营销 KPI 为什么变化，通常属于 A，而不是 B。
- 预算优化：是 B/C 结论的应用动作，不是单独的归因口径。

### B 分支常用业务指标

常用业务指标如下：

| 缩写/术语 | 中文含义 | 使用边界 |
|---|---|---|
| LTV | 用户生命周期价值 | 用于长期收益口径；需说明观察窗或预测窗 |
| CAC | 获客成本 | 常与 LTV 配套看回本，不等于增量成本效益 |
| ROI / ROAS | 投资回报 / 广告支出回报 | 可作效率指标；无实验校准时不得写成因果增量 |
| iROAS | 增量广告支出回报 | 必须有增量实验或等价因果对照支持 |
| CTR / CVR | 点击率 / 转化率 | 是中间行为指标，不能直接替代最终业务结果 |
| uplift / lift | 干预相对对照带来的提升 | 在 B3 中作为校准信号；若要做个体异质效应，转 C/33 |

## 3. 按候选因素、数据粒度与决策目标路由（必走）

前置：Phase 2 已判定为 B 口径。

**编号**：B0 / B1 / B2 / B3 分别对应 [20a](20a_general_contribution_allocation.md) / [21](21_mta_multi_touch.md) / [22](22_mmm_marketing_mix.md) / [23](23_incrementality_testing.md)（与第 2 节表一致）。

1. 候选因素是资源、功能、团队、流程节点、内容位等非营销实体，且目标是对既定结果分摊功劳 → 先走 B0（[20a](20a_general_contribution_allocation.md)）。
2. 有 user-level path / touchpoint / conversion path / 触点序列，且目标是把功劳分到触点或触达单元（营销里常等于渠道/素材战术优化）→ 先走 B1（21）。
3. 只有周/月/geo/渠道或资源聚合数据，目标是效率评估、边际贡献或预算/资源分配 → 先走 B2（22）。
4. 需要增量标尺、需要校准 B0/B1/B2、或要测 lift / iROAS / incremental contribution → 必须走 B3（23），并在需要因果结论时接 C。

叠加规则：
- B0/B1/B2 的结果要用于大额预算、资源、人力或 ROI 决策时，默认叠加 B3 或 C 做校准。
- 同时具备路径数据和聚合数据时，建议 B1 + B2 并行，再由 B3 统一尺度。

## 4. B 路径常见输入与输出

### 常见输入
- 已发生结果：要分配的总结果、结果窗口、结果口径（如总转化、GMV、产出、成本节省、用户留存）。
- 候选贡献因素：触点、渠道、资源、产品功能、内容位、团队、流程节点、模块等。
- 参与证据：路径日志、事件记录、使用记录、投入记录、产出映射、依赖关系或归属规则。
- 用户路径数据：user id（或匿名 id）、触点序列、时间戳、转化事件、lookback window。
- 触点日志：search/display/social/video/email/app/cross-device 相关曝光、点击、到达记录。
- 聚合面板：channel/resource spend、投入量、impressions/clicks + KPI（sales/conversions/revenue）+ controls。
- 实验数据：geo/campaign 级 treatment-control 设置、实验窗口、response metric。

### 常见输出

| 输出类型 | 典型指标 | 默认结论定位 |
|---------|---------|------------|
| 通用功劳分配 | contribution share、allocated value、组件/资源/功能贡献排序 | 规则或模型依赖 credit |
| 路径功劳分配 | first/last/linear/time-decay/U-shaped baseline、Markov removal effect、Shapley credit | 相关性 credit |
| 聚合贡献估计 | contribution、ROAS、mROAS、边际贡献 | 模型依赖估计 |
| 增量标尺（B3） | lift、iROAS、incremental sales | 增量对照估计；因果表述收口见 C |
| 决策处方 | 预算/资源重分配建议、场景对比 | 需附不确定性与敏感性 |
| 结论强度 | 候选解释 / 校准后结论 / 增量对照 | 由 Phase 6 校准结果决定 |

## 5. 默认分析顺序（"先快后准"策略）

**本节阅读说明（面向自动化执行）**：

- **第一至四步**是 B 路径常规流程；按数据条件子集执行即可，例如仅有路径数据时不强行做 MMM。
- **第三步的 B3 校准**只表示使用增量测试校准 B0/B1/B2 的方向、尺度或行动阈值；不等同于已进入完整 C 分支。
- **第五步仅在同时满足触发条件时执行**：B 结果将用于预算/资源/ROI 类决策，且任务已实际进入 C 分支并固定识别策略。否则跳过第五步。
- **分轨交付**表示同时给出 B 的功劳/模型估计与 C 的增量因果估计两组数字，不合并成单一「因果 MTA」数字。

### 第零步：固定功劳口径
- 固定结果：要把哪个已发生结果分出去，观察窗是什么，总量是否可加。
- 固定候选因素：哪些因素有资格分功劳，是否互斥，是否允许重叠或协同。
- 固定分配单位：按用户、会话、订单、项目、资源、功能、渠道、周/月等哪个层级分。
- 固定结论边界：是否只是 credit allocation，还是要进入 C 分支讨论增量。

### 第一步：先上 baseline（快）
- B0：进入 [20a](20a_general_contribution_allocation.md)，先做透明规则、活动量分摊或结果映射分摊。
- B1：先做 first-touch、last-touch、linear；有时间戳可加 time-decay；需强调首触与末触时可加 position-based/U-shaped，但必须声明权重。
- B2：先做简化 MMM（含最基本 controls）。
- 目的：快速暴露口径与数据问题，不把 baseline 当最终预算依据。

### 第二步：再做数据驱动方法（准）
- B0：若存在交互或重叠贡献，按 [20a](20a_general_contribution_allocation.md) 选择 Shapley / 回归分解 / constrained allocation / optimization-based allocation；必须写清可加性、交互项与约束。
- B1：规则 baseline 与时间相关建模分轨见 [21](21_mta_multi_touch.md)。无可靠时间戳或只关心路径结构时走第 5 节（Markov / Shapley 等）；有可靠时间戳且关心时滞与删失时走第 6 节（survival / point-process）。若要写代码，分别进入 [21a](21a_mta_baseline_implementation.md)、[21b](21b_mta_markov_shapley_implementation.md)、[21c](21c_mta_time_aware_implementation.md)。
- B2：进入含 carryover/adstock + saturation/shape 的 MMM，必要时用分层贝叶斯。

### 第三步：用 B3 做增量校准（更准）
- 使用 geo experiment / conversion lift / budget split 估计 lift 或 iROAS。
- 把实验信号回流校准 B0/B1/B2 的方向、尺度与可行动阈值。

### 第四步：三角校验并收敛
```
B0/B1/B2 功劳分配 ←→ 聚合建模 / 替代分配规则 ←→ 增量测试结果
    方向一致？→ 提高结论强度
    不一致？→ 排查口径、假设、数据覆盖与外推边界
```

### 第五步（条件步骤）：B 与 C 分轨输出

**仅当同时满足时执行**：B 产出将用于预算 / 资源 / ROI 类决策，且任务已按 [30](../c/30_causal_attribution_overview.md) 实际进入 C 分支并固定识别策略。仅停留在 23 的 lift/iROAS 读数、未按 30 固定识别策略的，**不触发**本步，按 B 口径输出即可。

触发后须守住的边界：

1. **B 与 C 分开给**：B 给功劳 / 模型估计，C 给增量效应及区间；不得合并成单一「因果 MTA」数字，也不得把 C 的总增量按 B 份额摊成细粒度因果功劳。
2. **先固定 C 的 estimand 与识别假设**（treatment、对照、分析单元、时间窗、主估计量如 ITT/LATE/ATT），再给 C 估计；取舍顺序见 [30 §1.5](../c/30_causal_attribution_overview.md)。
3. **不外推**：单次 geo / campaign / 资源实验不能覆盖未实验的渠道、人群、功能或时期；需要外推时降结论强度并写明边界。
4. **方法不一致时**按 [51](../core/51_evaluation_calibration.md) 三角校验排查：口径与时间对齐 → 数据覆盖与选择偏差 → 混杂与 SUTVA/溢出 → B 侧模型与归因规则假设 → 再决定降级 B 或补做 C。
5. 结论强度与口径边界统一按 [52](../core/52_output_guardrails.md)。

## 6. 关键警告

1. B 分支是已发生结果下的功劳分配，不等于变化解释，也不等于因果增量。
2. MTA / DDA 是路径级功劳分配，不等于因果增量。
3. MMM 是聚合层贡献估计与预算优化，不等于实验真值。
4. 增量测试是 B 路径的增量标尺，常用于校准 B0/B1/B2 的方向与尺度，而非可选装饰。
5. 若缺少校准证据，输出应定位为"相关性分配/模型估计"，而不是"确定的增量 ROI / 因果贡献"。
6. 平台策略、资源配置或团队行为会随归因反馈而变化（反身性）；缺少持续校准时，历史规律可能失效。

## 7. 输出收口

B 分支按调用方契约输出；结构化输出边界见 [52_output_guardrails.md](../core/52_output_guardrails.md)。

如果没有校准证据，就把结论写成相关性功劳或模型依赖估计；不要把 B 分支结果写成确定的增量因果结论。
