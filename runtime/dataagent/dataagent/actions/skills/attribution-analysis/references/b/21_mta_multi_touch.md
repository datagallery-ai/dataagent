# B1. 多触点归因（MTA）

**定位**：B 分支下「**用户级路径/触点功劳**」；下文以 **search/display/social** 等**营销触达**为例，同类序列数据也可用于其他多步漏斗。

## 1. 适用前提

- 有用户级路径数据，目标是分配 search/display/social/video/email/app/cross-device 等触点功劳。
- 业务问题是"路径上谁拿 credit"，而不是"两期指标差值由谁解释"。
- 结果主要用于战术层优化：渠道（在哪里触达）、素材（用什么内容触达）、频控（多久/多少次触达）、触达顺序（先后路径）。

**没有路径级数据时不要声称做了 MTA**，应降级到 A 分支或 B2（MMM）。

## 2. 入场前确认清单（先把口径锁死）

| 检查项 | 最低要求 | 不满足时处理 |
|------|---------|------------|
| 转化定义 | 明确一次转化（购买/注册/下载）及去重规则 | 不清晰则停在口径对齐 |
| lookback window | 明确 7/28/90 天等窗口并固定 | 先做多窗口敏感性比较 |
| 路径完整性 | 同时有转化路径与未转化路径 | 仅转化路径时降低结论强度 |
| 时间信息 | 至少有触点顺序；最好有时间戳 | 无可靠时间戳时不可用 time-decay 与第 6 节（方法三）；data-driven 方法见第 5 节（方法二） |
| 跨设备拼接 | 能说明 user stitching 规则与漏配比例 | 漏配高时标注"路径不完整" |

## 3. 默认执行流程（先快后准）

**方法区只分三节**，对应后文三个二级标题；下表中的“第 N 节”就是文件里的 `## N.` 标题：

| 方法区 | 对应二级标题 | 是什么 | 包含哪些方法 |
|----|------|--------|-------------|
| **方法一** | `## 4. 方法一：规则 baseline（只作基线）` | 人工固定权重，只作基线 | First-touch、Last-touch、Linear、Position-based/U-shaped；**Time-decay 仅在有可靠时间戳时可用**；实现见 [21a](21a_mta_baseline_implementation.md) |
| **方法二** | `## 5. 方法二：Data-driven sequence-only（顺序信息为主，不依赖可靠时间戳）` | 由数据估路径功劳；**不依赖**可靠时间戳 | Markov + removal effect、Shapley、Logistic/Probabilistic 路径模型；实现见 [21b](21b_mta_markov_shapley_implementation.md) |
| **方法三** | `## 6. 方法三：Timestamp-aware（显式使用时间、时滞与删失）` | 显式用时间戳建模**时滞、删失**等 | Time-to-event / Survival MTA、Poisson/Point-process MTA；**不含 time-decay**；实现见 [21c](21c_mta_time_aware_implementation.md) |

执行顺序用 **动作 A–D** 表示，避免和 `## 4/5/6` 的章节编号混淆：先做动作 B 的 baseline（方法一），再按动作 C 的数据条件，在方法二与方法三中选择 data-driven 路径；第 7 节起负责结果收口、护栏和衔接。

### 动作 A. 固定归因口径
- 确认转化定义、lookback window、路径截断规则、是否去重触点。
- 明确 customer-initiated 与 firm-initiated 触点口径，避免把内生性混在一个结论里。
- 明确归因对象是 channel、campaign、creative、touchpoint 还是位置段（首触/中间/末触）；粒度不同不得直接相加比较。

### 动作 B. 规则 baseline（必须）
- 见第 4 节（方法一：规则 baseline）。先跑 first-touch、last-touch、linear；若业务关心首触启动与末触促成，可加 position-based / U-shaped；**time-decay 仅在可靠时间戳可用时**加入。
- 这些规则的作用是快速暴露路径结构与口径问题，不是证明真实贡献。
- **first/last/linear/time-decay/position-based 都只能做 baseline，不应默认作为最终结论。**

### 动作 C. 再进入 data-driven MTA（优先于固定规则）

| 数据条件 | 主路径 | 说明 |
|---------|------|------|
| 只有顺序，时间戳缺失或质量差 | 第 5 节（方法二：Data-driven sequence-only） | 用 Markov / Shapley / probabilistic 路径模型 |
| 有可靠时间戳，关心时滞与删失 | 第 6 节（方法三：Timestamp-aware） | 用 time-to-event / survival / Poisson 或 point-process |

### 动作 D. 收敛结果并与 baseline 对比
- 输出 channel/touchpoint credit、关键路径洞察、与 baseline 的差异。
- 差异过大时优先排查口径、路径截断、跨设备漏配、删失处理。

### 代码实现最小契约（写代码前必须固定）

若任务要求实现本文件中的方法，先固定以下契约；缺少任一项时先向用户确认，或在代码中以显式参数暴露，不得在实现里静默脑补。

| 契约项 | 必须固定什么 | 默认建议 |
|------|-------------|---------|
| 分析单元 | user、session、path 还是 conversion path | 默认 `path_id` 一行一条路径 |
| 触点粒度 | channel、campaign、creative、touchpoint 或其它层级 | 默认先聚合到 channel；更细粒度需样本足够 |
| 路径表示 | 是否保留重复触点、是否按时间排序、是否截断 lookback window | 默认保留重复触点并按时间升序；没有时间戳时按输入顺序 |
| 结果字段 | conversion flag、conversion value、conversion time、non-conversion/null | 至少需要 `converted`；有价值口径时用 `value` |
| 未转化路径 | 是否纳入 | Markov / probabilistic / survival 默认必须纳入；仅转化路径时降低结论强度 |
| 输出归一化 | raw effect/credit 是否转成 share | 默认同时输出 `raw_credit` 与 `normalized_credit`；标准化分母必须写清 |

推荐输入表最少包含：

```text
path_id, user_id(optional), touchpoint, touch_order, touch_time(optional), converted, value(optional)
```

推荐输出表最少包含：

```text
method, entity_type, entity_id, raw_credit, normalized_credit, value_scope, assumptions, warnings
```

实现验收：
- 任一方法都必须保留参数与口径说明，特别是 lookback window、是否去重、是否纳入未转化路径。
- baseline 方法的 `normalized_credit` 应能加总到 1（或 100%）；细则见 [21a](21a_mta_baseline_implementation.md)。
- Markov / Shapley / probabilistic 路径模型的实现验收见 [21b](21b_mta_markov_shapley_implementation.md)。
- Time-to-event / point-process 的实现验收见 [21c](21c_mta_time_aware_implementation.md)。
- 无可靠时间戳时，代码必须拒绝 time-decay、survival、point-process，或降级到第 5 节方法二。

## 4. 方法一：规则 baseline（只作基线）

| 方法 | 权重分配 | 适用场景 | 主要风险 |
|------|---------|---------|---------|
| First-touch | 100% 给首个触点 | 拉新、首触来源、品牌/获客入口粗评估 | 忽略后续促成与转化前触点 |
| Last-touch | 100% 给末个触点 | 短路径、强转化导向、下单前最后入口观察 | 系统性偏向品牌词、再营销、导购类末端触点 |
| Linear | 路径内触点均分 | 需要中性 baseline，或路径较短且缺少更多假设 | 把明显不同作用的触点视为等价 |
| Position-based / U-shaped | 常见为首触 40%、末触 40%、中间合计 20%；比例可按业务改写但必须声明 | 同时强调启动与促成，且中间触点只作辅助观察 | 权重是人为设定；不能把 40/20/40 写成数据发现 |
| Time-decay | 越接近转化权重越高（**仅有可靠时间戳时可用**） | 周期短、转化前近期触点确实更关键 | 低估早期种草、品牌和长周期影响 |

执行要求：
- 至少输出 first-touch、last-touch、linear 三个 baseline 的对比；**有可靠时间戳才输出 time-decay**。
- 若使用 position-based，必须写明采用的比例（如 40/20/40）与业务理由；不得默认所有行业都适合 U 型。
- baseline 与 data-driven 结果差异大时，先排查路径截断、去重、跨设备漏配、lookback window，再解释业务含义。
- time-decay 不代替第 5 节的 data-driven 方法或第 6 节的时间建模方法；仅作快评估，不代替 23 校准。

代码实现细则见 [21a_mta_baseline_implementation.md](21a_mta_baseline_implementation.md)。若脚本在 baseline 分配、time-decay、U-shaped 边界条件上写错，只补充 21a，不在本主文件堆长代码。

## 5. 方法二：Data-driven sequence-only（顺序信息为主，不依赖可靠时间戳）

| 方法 | 适用数据粒度 | 输入要求 | 关键假设 | 何时优先用 | 何时不要用 | 输出是什么 | 与 22/23 如何衔接 |
|------|------------|---------|---------|-----------|-----------|-----------|------------------|
| Markov + removal effect | user-path 序列 | 渠道序列、转化/未转化终点、足够路径样本；通常含 start、conversion、null/exit 状态 | 下一步主要由当前状态或低阶历史决定；转移概率可稳定估计 | 想看"移除某渠道后整体转化概率下降"；渠道顺序重要 | 路径极稀疏、状态过多、跨设备缺失严重；只有转化路径且缺少未转化终点 | 渠道移除效应与标准化相对 credit | 与 22 对比渠道排序；预算重分配前用 23 校准尺度 |
| Shapley 路径分配 | path 或 channel coalition | 可构造子集价值函数，触点数可控；价值函数可用转化率、转化数、收入或净收入 | 子集价值可稳定估计；按所有加入顺序的边际贡献做公平分配 | 需要公平可解释分配、用于跨团队对齐口径；触点共同正向作用明显 | 触点过多且无法先聚合，样本过稀导致子集不稳；价值函数口径无法固定 | 渠道/触点 Shapley credit | 与 22 对比长期贡献；重要预算动作需 23 验证 |
| Logistic/Probabilistic 路径模型 | path-level 观测样本 | 路径特征、转化标签、基础协变量 | 函数形式可近似主要关系，残差可接受 | 需要稳定预测与可解释路径特征贡献 | 强非线性且样本不足，或特征共线性失控 | 概率贡献、路径特征影响方向 | 可把聚合贡献与 22 对齐；对因果主张仍需 23 |

代码实现细则见 [21b_mta_markov_shapley_implementation.md](21b_mta_markov_shapley_implementation.md)。若脚本在 Markov removal、Shapley value function、采样近似、probabilistic ablation 上写错，只补充 21b。

## 6. 方法三：Timestamp-aware（显式使用时间、时滞与删失）

适用于有可靠时间戳、需要刻画触点时滞或转化前未转化删失的场景；**规则 baseline 中的 time-decay 见第 4 节，不属于本节**。

| 方法 | 适用数据粒度 | 输入要求 | 关键假设 | 何时优先用 | 何时不要用 | 输出是什么 | 与 22/23 如何衔接 |
|------|------------|---------|---------|-----------|-----------|-----------|------------------|
| Time-to-event / Survival MTA | user-time 路径 | 时间戳、转化时点、未转化删失标记、协变量 | hazard 结构可近似，删失机制可解释 | 路径长、关心触点时滞和删失 | 时间戳误差大、窗口过短、事件太少 | 时变影响、时滞效应、credit 分配 | 结果可映射到 22 的周期贡献；大额决策仍需 23 |
| Poisson/Point-process MTA（可借鉴思路） | 细粒度事件流 | 高频触点事件、转化事件流、时间分辨率稳定 | 事件过程可由强度函数描述 | 高频广告触达、需要细时间刻度响应 | 事件记录不全、归因窗口不稳定 | 触点强度与转化关联贡献 | 作为高阶分析补充，预算结论建议用 23 定标 |

代码实现细则见 [21c_mta_time_aware_implementation.md](21c_mta_time_aware_implementation.md)。若脚本在删失处理、hazard 数据展开、point-process 时间粒度或 ablation 上写错，只补充 21c。

## 7. MTA 结果要素

1. 触点/渠道 credit（含 baseline 与 data-driven 对比）。
2. 路径洞察（高转化路径、低效路径、触点顺序与时滞特征）。
3. 结果边界（相关性定位、潜在偏差、数据缺口）。
4. 与 22/23 的后续动作建议（是否需要策略层预算模型或实验校准）。

## 8. Guardrails

本节只补充 MTA 的专项风险与输出边界；结构化输出边界见 [52_output_guardrails.md](../core/52_output_guardrails.md)。

1. MTA 默认是相关性 credit，不自动解释为因果增量。
2. customer-initiated 与 firm-initiated 触点可能存在内生性/选择偏差，必须单独声明。
3. cross-device path 不完整时，结论强度至少下调一级。
4. 若归因结果用于大额预算决策，应优先寻求 lift/experiment（23）校准。

## 9. 与 22/23 的衔接规则

- 需要跨渠道长期预算配置（含线下）时，从 21 衔接到 22。
- 21 与 22 方向不一致，或预算影响较大时，优先进入 23 获取因果标尺。
- 23 返回后，不直接把实验值机械替换 MTA credit，而是用于限制解释口径与决策阈值。
