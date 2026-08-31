# C 分支：因果归因 — 总览

本文件是因果归因的入口。目标是识别和估计干预的因果效应。C 分支与其他分支的核心区别是：显式声明因果识别假设，并验证其合理性。扩展词条（如 **SUTVA**）见 [glossary.md](../shared/glossary.md)。

> **使用顺序**：先用 §1.5 决策树锁定识别策略，再按策略只读 32 对应小节（§2a + §5.x）。

## 1. 范围

- Phase 2 主分支须为 **C** 后使用本文件。
- 分支口径对照见 [01 问题分类](../core/01_problem_classification.md) 与 `SKILL.md`。
- C 分支执行内容自 §1.5 起。
- 输出措辞按 C 口径；禁止用 A / B 口径冒充业务因果结论，见 [52](../core/52_output_guardrails.md)。

## 1.5 识别策略决策树（动笔前必做）

按以下顺序逐条判断，命中即定策略；不要在估计阶段才回头改策略。若题面或数据已明确给出工具变量 / encouragement（如 `*_close_win`、lottery、random invitation），优先按第 2 步走 IV，不要只因存在阈值变量就改判 RDD。

```
1. 数据中是否存在"行政/规则切分阈值的连续变量"（age≥65、score≥cutoff、population≥1500）？
   → RDD
   1a. 处理 D 在 cutoff 处是否"确定性 0/1 跳变"？
       → sharp RDD：cutoff 处不存在“遵从者”子群体，识别的是**阈值处的条件平均处理效应**，
         默认记作 CATE（即 X=cutoff 处的 ATE）；**不要套用遵从者口径的 LATE/CLATE**
   1b. 跳变只是概率提升（compliance < 1）？
       → fuzzy RDD：在 cutoff 处用 IV 逻辑还原遵从者平均处理效应，
         记作 LATE = reduced-form / first-stage（Wald 比值）
       （详见 32 §5.3 的 fuzzy 强制清单）
       若这种概率提升来自题面明确指定的独立 instrument 列，而不是单纯由 running variable
       构造的 cutoff assignment，回到第 2 步按 IV / encouragement 处理。

2. 是否存在"外生分配 + 不完全遵守"的设定（lottery、随机邀请、encouragement）？
   → IV / encouragement design，估计 LATE
   - 必须能写清楚 instrument 的 (a) 相关性 (b) 排除性 (c) 单调性
   - 详见 32 §5.2

3. 是否有"前后期 + 处理组/对照组 + 平行趋势"的面板结构？
   → DID，估计 ATT
   - staggered adoption 切到 Callaway-Sant'Anna
   - 详见 32 §5.1

4. 是否有真随机分组（A/B test），且随机化在分析样本中仍可直接使用？
   → RCT，估计 ITT 或 ATE
   → 进入 31_experimental_methods.md
   - 默认优先级：**RCT 只在"随机化可直接使用"时成立**；观测数据 + 一个 treatment 列的默认是
     DiD / IV / RDD / CE 之一，**不是 RCT**。给 RCT 这个标签前必须先过 31 §1 的随机化质量闸门。
   - 警告：数据里有 treatment 列、或背景称"这是一个实验/RCT"，都不等于随机化在当前样本仍成立。
     若存在差异性流失、系统性不合规、明显的协变量不平衡，或必须靠协变量调整才能恢复可比性，
     则随机化不可直接使用 → 不要标 RCT，转第 5 步走 Conditional Exogeneity。

5. 以上都不命中，但有丰富 pre-treatment 协变量？
   → Conditional Exogeneity（PSM / IPW / DML），估计 ATE/ATT
   - 结构化契约先给主估计；SMD + overlap + 敏感性只作诊断补充
   - 详见 32 §5.4

6. 都不命中？
   → 不要硬做因果推断。降级到 A 分支输出"候选解释"。
```

### 估计量由识别策略决定（动笔即定，三处一致）

锁定策略后，`causal_quantity` 不是自由发挥项，而是由识别设计决定。下表是各策略的**默认估计量**；偏离默认必须有数据/识别上的触发条件（如同质效应假设、目标人群改变）：

| 识别策略 | 默认估计量 | 说明 / 不要写成 |
|---|---|---|
| DiD | **ATT** | DiD 识别的是**处理组**的平均处理效应；**默认不是 ATE**，除非能论证处理效应同质或分配随机 |
| sharp RDD | **CATE**（阈值处条件 ATE） | cutoff 处不存在“遵从者”子群体，**不要写 LATE/CLATE** |
| fuzzy RDD | **LATE** | = reduced-form / first-stage（遵从者口径） |
| IV / encouragement | **LATE** | 遵从者口径；**不要写成总体 ATE** |
| RCT（随机化可直接用） | **ATE**（不合规时 ITT，再 CACE/LATE） | 仅当随机化未被破坏 |
| Conditional Exogeneity | **由目标人群决定**：对被处理者→ATT；对全体→ATE；对未处理者→ATC | 不要无脑填 ATE；按问句确定目标人群 |

> 估计量名称须在**因果问句、主估计、结论**三处一致；与上表错配（如 DiD 报 ATE、sharp RDD 报 LATE、IV 报总体 ATE）按 §7 反模式处理。

**反例**（常见踩坑模式）：

- 数据里已有合规的工具变量列，却仍然回退到 OLS-FE → 应停在第 2 步直接走 IV
- 处理变量是 fuzzy compliance（实际 compliance 概率 < 1），却用 sharp RDD 估 → 应停在第 1b 直接走 fuzzy + first-stage 反算
- RDD 默认套全样本二次/三次多项式，得到不显著结论 → RDD 默认主估计应是 CCT/IK 带宽下的局部线性，多项式只能进 robustness 不能作为主估计
- 背景声称"随机实验"，但分析样本有差异性流失或协变量不平衡，却仍直接套均值差当 RCT → 随机化已不可直接使用，应改走 conditional exogeneity，用协变量调整恢复可比性

## 2. 什么时候必须用 C 分支

- 需要回答"干预导致了多少增量"
- 需要据此做预算/策略/产品决策，且决策风险高
- A/B 的"候选解释"不够，需要因果证据
- 需要区分"相关性归因"和"增量归因"

## 3. 开始前必须写出的因果问句

> 估计什么效应？（ATE / ATT / CATE / 政策价值）
> 识别假设是什么？（可忽略性 / 重叠 / SUTVA / 无干扰）
> 反事实是什么？（如果没有干预会怎样）

写不出因果问句 → 不要做因果推断。降级到 A 分支做变化解释。

### 识别与估计要分开写

- **识别（identification）**：先说明为什么现有数据能识别目标效应，核心是反事实从哪里来、允许控制哪些变量、哪些变量绝不能控
- **估计（estimation）**：在识别策略已经站住的前提下，再决定用均值差、回归、2SLS、局部线性、AIPW 等什么估计量，以及如何给标准误和区间
- **顺序不能反**：估计器再高级，也补不上识别假设的缺口；先把 design 讲清楚，再谈 estimator
- 因果归因里最常见的错误不是不会算，而是把策略、控制集、post-treatment 变量和策略专属字段搞混

## 4. 二级分类

| 条件 | 子方向 | 详细文件 |
|------|--------|---------|
| 有随机分组（A/B Test） | 实验方法 | [31_experimental_methods.md](31_experimental_methods.md) |
| 无随机分组，观测数据 | 准实验方法 | [32_quasi_experimental_methods.md](32_quasi_experimental_methods.md) |
| 需要分人群/分层异质效应 | 异质效应 | [33_heterogeneous_effects.md](33_heterogeneous_effects.md) |

补充说明：本 skill 的因果分支里，高频识别策略可收敛为五类：RCT、Conditional Exogeneity、Instrumental Variable、Regression Discontinuity、Difference-in-Differences。本 skill 中：

- RCT 进入 [31_experimental_methods.md](31_experimental_methods.md)
- Conditional Exogeneity / IV / RDD / DiD 进入 [32_quasi_experimental_methods.md](32_quasi_experimental_methods.md)
- 需要进一步做 CATE / uplift / policy learning，再进入 [33_heterogeneous_effects.md](33_heterogeneous_effects.md)

## 5. 默认流程

1. 写出因果问句与识别假设
2. 先定 identification strategy，再定 estimator 与 standard error
3. 明确 treatment / outcome / estimand / 最小控制集 / 坏控制。**最小控制集来自因果图与题目/文献设定，不是默认空集**：FE/cutoff 只吸收时间不变混杂或断点处连续性，随时间变化的混杂、或设定明确要求的 pre-treatment 协变量仍须进入主估计。结构化输出的 `controls` 须等于主估计回归实际纳入的 pre-treatment 协变量，不要因「默认空控制集」就填 null（漏填与过度控制是两头都要避免的错）。
4. 判断是否有实验/随机化 → 31 或 32
5. 选择识别策略
6. **按 [32 §2a](32_quasi_experimental_methods.md) 拿主估计**（effect + SE）
7. **有结构化契约 → 按 [52](../core/52_output_guardrails.md) 交付**（如裸 JSON）
8. 需要分层 → 33

> **简单优先**：识别策略选定后，复杂建模（FE 嵌套、Bayes、DML 等）不是默认；只有当最小配方明确不可识别或偏估时再上，并写明原因。复杂 estimator 解决不了识别假设的缺口。

## 6. 关键警告

### "假设是一等公民"
对因果归因而言，宁可明确不可识别，也不要用复杂模型掩盖假设缺口。

### "可观测 ≠ 可识别"
有日志不等于能估计因果贡献。关键在于是否有可接受的识别假设，或是否能设计对照/实验。

### 识别假设不成立时
结论无效——不如不做。降级为 A 分支的"候选解释"。

## 7. 专项流程自检（执行层硬规则）

C 分支任务出现下表任一情况时，必须按「对应硬规则」改正。本表只约束因果识别与估计，不约束机器可读输出格式。

| 反模式 | 触发信号 | 对应硬规则 |
|---|---|---|
| **自造 treatment** | 把事件名、时间区间、业务术语当作处理变量，没有对应到真实数据列或可复现构造 | 必须用真实列名或写清派生规则 |
| **没标估计量** | 全文找不到 ATE / ATT / CATE / LATE / CLATE / ITT 任一名词 | 估计量名称须在问句、主估计与结论三处一致，且**与识别策略匹配**：sharp RDD = 阈值处 CATE（非 LATE）、fuzzy RDD / IV = 遵从者的 LATE、DiD = ATT、RCT = ITT 或 ATE |
| **估计量与策略错配** | sharp RDD 却报 LATE/CLATE；DiD 却报 ATE；IV 报总体 ATE | 估计量必须由识别设计决定（见上一行匹配规则），不能凭"听起来像因果量"的习惯随手填 |
| **观测数据误判为 RCT** | 有 treatment 列或背景自称"实验"，未过随机化质量闸门就标 RCT；或需协变量调平衡才可比却仍按 RCT 均值差 | 随机化不可直接使用时不得标 RCT，按 §1.5 第 4→5 步改走 Conditional Exogeneity（或 DiD/IV/RDD），用协变量调整恢复可比性 |
| **声称 IV 却用 OLS 估计** | 「识别」段写工具变量（IV），「估计」段却用 OLS 固定效应（OLS-FE） | 须用两阶段最小二乘（2SLS），并贴**第一阶段、约化式、LATE** 三行数字以便核对 |
| **把约化式效应当成 LATE** | IV 设定下，输出的效应与 ITT 几乎相等 | 须校验 `|λ̂ − β̂/α̂| / |λ̂| < 1%`，否则视为实现错误 |
| **把 RDD 做成全样本均值差** | RDD 任务没有带宽、没有断点附近局部回归 | 使用 CCT/IK 带宽 + 断点局部线性作为主估计；带宽稳健性只作诊断补充 |
| **效应量级反常** | `|effect| > 5 × std(结果变量)` 或超出结果变量合理取值范围 | 判为一致性自检失败，结论降级为「待人工复核」 |
| **标准误过大错杀显著性** | 相对仿真/审计真值下的标准误（GT），SE 系统性偏大，把本应显著说成不显著 | 有面板组维度时须用组内聚类标准误等合适设定，禁止只用默认 OLS 的标准误 |
| **把「不显著」说成边际显著** | p 在 0.05–0.2，却写「边际显著」「marginally significant」「提示性证据」 | 一律改为「方向一致但统计上不能拒绝零假设」；效应、标准误、置信区间与 p 值只作诊断补充 |
| **弱第一阶段却采信 LATE** | 模糊 RDD 的第一阶段跳跃相对约化式过小 → LATE 数值爆炸 | 须写明弱第一阶段；补充 **Anderson–Rubin 置信区间（AR CI）**；不得把该 LATE 写进主结论 |
| **RDD 主估计随意平均多带宽或用高阶多项式** | 主结果用多个带宽的均值或把高阶多项式当默认 | 主估计 = 单一 CCT/IK 带宽下的局部线性；其余规格只作稳健性对照 |
| **不当控制变量** | 控制集含处理后出现的变量、中介、对撞元或与处理机械相关的变量 | 控制变量须先经过 bad-controls 四项预检并写明剔除原因 |
| **异质性（HTE）被滥用** | 子样本 n < 30 仍单独输出 CATE；仅凭某一子群 p < 0.05 断言异质性；主效应不显著却用子群效应决策 | 禁止单靠子群 p 值断言异质性；须交代方法、最小子群规模、样本重叠与政策相关指标；主 ATE 不显著时子群效应只作探索性 |
| **多套估计并列却不指定主结果** | 多种方法或多个带宽并排，不说明以哪一行为准 | 必须写明主估计及其选取理由，其余仅作稳健性对照 |
| **用非等价路径否定主估计** | 用 group-means 否定 staggered/多期 TWFE 等**错误对账** | 对账路径须与主估计器等价；**没做对账不是反模式**，有契约时仍须先交付主估计 |
| **看到基线不平衡就加协变量** | DiD/RDD 等已由 FE 或 cutoff 识别，仍以"baseline imbalance"为由追加 baseline 控制 | 先回到平行趋势/连续性假设讨论；加协变量必须先走 §5.4 bad-controls 预检并写明是 pre-treatment |

31/32/33 已覆盖如何选识别方式、如何估计与如何做稳健性检查。若调用方要 JSON/schema，只能按其 prompt 另行约定字段。

## 8. 结果收口

见 [52](../core/52_output_guardrails.md)。
