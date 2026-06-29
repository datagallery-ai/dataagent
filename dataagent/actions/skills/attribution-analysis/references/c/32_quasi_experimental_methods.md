# C.2 准实验方法

> **导航**：已在 [30 §1.5](30_causal_attribution_overview.md) 锁定策略后，只读对应策略的 §2a 配方 + §5 硬规则。

## 1. 适用前提

没有随机分组，需要从观测数据中估计因果效应。**识别假设的合理性是核心**——假设不成立则结论无效。

### 先分清识别与估计

- **识别问题**：反事实比较来自哪里，为什么这组对照在目标假设下可用
- **估计问题**：在给定识别策略下，用什么估计器与标准误实现该比较
- 在准实验里，最常见的失败不是“不会回归”，而是控制集错了、把 post-treatment 变量当控制、或把识别策略专属信息搞混
- 在本 skill 的准实验分析里，至少要显式说明：strategy、causal quantity、treatment、outcome、controls，以及 IV/RDD/DiD 的专属识别信息

## 2. 方法选择决策

| 你有什么 | 推荐方法 | 关键假设 |
|---------|---------|---------|
| 丰富协变量 + 截面数据 | 匹配/加权（PSM/IPTW） | 可忽略性（无未观测混杂） |
| 面板数据 + 自然实验 | DID（双重差分） | 平行趋势 |
| 明确政策阈值 | RDD（断点回归） | 连续赋值变量 |
| 有效的外生工具变量 | IV（工具变量） | 排除性约束 |
| 少数处理单位 + 丰富对照 | 合成控制 | 可拟合性 |

## 2a. 默认最小估计配方

> 识别策略选定后，先用本节拿到 **effect + SE**。有结构化契约（如 JSON）时，主估计出来就按 [52](../core/52_output_guardrails.md) 交付；McCrary、多带宽、互核对账等稳健性**可选**，工具不可用（如 `rddensity`）则跳过，不得因此不交付。

### DiD：先判定面板形态，再选最小等价实现

```text
先判定：
1. 简单 2x2（单一处理时点、吸收性 treatment、平衡 pre/post）：
   样本：限定到 {pre_period, post_period} 两期；任一期 outcome 缺失的单位剔除
   treat：跨期由对照切到处理的单位（cohort 定义）
   post：post_period == 1
   estimator：OLS  y ~ treat * post
   SE：cluster_by_group(group_variable)

2. 多期 / 异步进入 / 非吸收 treatment / 不平衡面板：
   样本：使用题目定义的可比面板样本，不要为了凑 2x2 任意丢弃有效时期
   estimator：y ~ treatment + group FE + time FE（或题目/论文明确要求的等价 DiD 规格）
   SE：cluster_by_group(group_variable)

默认控制：[] 仅当 FE 已吸收全部混杂；若需控制随时间变化的混杂、或题目/文献设定要求的 pre-treatment 协变量，则纳入主估计并据实填入 controls（见 30 §5 第 3 条）
输出：effect = 主处理系数；SE = clustered SE
```

**不要默认追加 baseline 协变量**。即便看到基线不平衡，**面板 FE 已经吸收时间不变的单位异质性**；要加协变量必须先走 §5.4 的 bad-controls 预检，且只能是 **pre-treatment**。

**staggered adoption**（多 cohort 不同时间进入处理）：若目标是 cohort-specific ATT，传统 TWFE 有负权问题，应切到 Callaway-Sant'Anna、de Chaisemartin-D'Haultfœuille 或 Sun-Abraham；若题目/原论文/输出契约明确要求的是当前 treatment 的 FE 系数，则可使用等价 TWFE 规格，但必须说明 estimand 口径。

### IV / 2SLS：默认三行对账表

```text
first-stage:  treatment ~ instrument (+ exogenous controls)     → 报 α̂、F
reduced-form: outcome   ~ instrument (+ exogenous controls)     → 报 β̂
2SLS:         outcome   ~ treatment_hat (+ exogenous controls)  → 报 λ̂ = β̂ / α̂
SE：聚类（若有面板）或 robust；弱工具 → Anderson-Rubin CI 而非 t-CI
默认控制：仅 pre-treatment 外生变量；不要把 instrument 或下游变量塞 controls
输出：主输出使用 2SLS / Wald LATE；点估计与名称（ITT/LATE/CLATE）三处一致
```

校验：`|λ̂ − β̂/α̂| / |λ̂| < 1%`；不通过 → 实现错误，回头查样本/控制集对齐。

### RDD：默认局部线性 + CCT/IK 单一带宽

> **`rdrobust` 出 effect+SE 后立即输出评测 JSON**；McCrary（无 `rddensity` 则跳过）、多带宽、互核任一项失败或跳过均不阻塞。

```text
样本：cutoff 附近 ±h_CCT 的局部数据
estimator：local linear（rdrobust 或等价实现；两侧分别拟合斜率）
带宽：CCT 或 IK 的 MSE-optimal 单值；不取多带宽平均
默认控制：[] —— RDD 的识别来自 cutoff 处连续性，加协变量不改变识别
estimand：sharp RDD = 阈值处的条件平均处理效应（默认记作 CATE，即 X=cutoff 处的 ATE），
          因 sharp 设定不存在“遵从者”子群体，**不是** 遵从者口径的 LATE；
          fuzzy RDD 才报 LATE（= reduced-form / first-stage 的 Wald 比值）
fuzzy：主输出使用 Wald/IV-LATE
输出：effect + SE
```

### Conditional Exogeneity（PSM/IPW/DML）：默认 IPTW 或 PSM + 平衡核查

```text
treatment model：propensity score（pre-treatment 协变量；prune 极端 PS）
estimator：IPTW 或 PSM 后的 outcome difference；进阶用 AIPW / DML 做双重稳健
默认控制：仅 pre-treatment 且非 mediator/collider/treatment 机械函数（详见 §5.4）
输出：effect + SE
```

**仅在 RCT / DiD / RDD / IV 都不可用**时才走 CE——CE 的识别假设最强，不应优先。

### 匹配/加权
- 这类识别策略也常记作 **Conditional Exogeneity**
- **方法定义**：在“给定处理前协变量后，处理分配近似可忽略”的前提下，用匹配、加权、回归调整或双重稳健方法恢复反事实
- **识别假设**：无未观测混杂、重叠性（propensity 不贴近 0/1）、控制变量必须是 pre-treatment 且不包含 mediator / collider
- **估计方式**：PSM、IPTW、outcome regression、AIPW / DML；实务上优先看 balance 与 overlap，再看点估计
- **常见误区**：把倾向评分模型的高 AUC 当成识别充分；控制了处理后的行为变量；极端权重不裁剪；匹配后只看显著性不看 SMD
- **结果校验**：匹配/加权前后都报 SMD；画 propensity overlap；做 trimming 稳健性；做未观测混杂敏感性分析（E-value / Rosenbaum bounds）

### DID
- **方法定义**：用处理组与对照组在干预前后的差分，再做一次差分，净掉共同时间冲击
- **识别假设**：核心不是“处理前水平相同”，而是**若无处理，潜在结果趋势应平行**；还要警惕预期效应、样本构成变化和其他同期政策
- **估计方式**：最简单是 two-period DiD；有多期数据时优先画 event-study。staggered adoption 下须先说明目标 estimand：若是 cohort ATT，优先考虑 Sun-Abraham、Callaway-Sant'Anna 或 dCDH；若题目/原研究定义的是当前 treatment 的 FE 系数，可用 TWFE，但要说明口径
- **常见误区**：只看处理后差异不看 pre-trend；把受处理影响的中间变量放进 controls；在异步处理时无说明地套 TWFE；用简单 group-means 去否定一个非等价的 FE/TWFE 主估计
- **结果校验**：干预前 event-study 系数应接近零；做假政策时点 placebo；做 leave-one-group-out；检查不同窗口长度下结论是否稳定

### RDD
- **方法定义**：利用 running variable 在 cutoff 附近导致处理状态跳变，把阈值附近个体视为近似随机分配
- **识别假设**：潜在结果在 cutoff 处连续；个体不能精确操纵 running variable；估计的是 cutoff 附近的局部效应
- **估计方式**：局部线性回归是默认起点，带宽和核函数要做敏感性分析；遇到阈值附近可疑操纵，可考虑 donut RD
- **常见误区**：用高阶全局多项式硬拟合；把 cutoff 两侧不可比样本拉得太远；把阈值后才出现的资格/申请行为当控制变量
- **可选诊断**：McCrary、多带宽、协变量连续性（均不阻塞交付）

### IV
- **方法定义**：用只影响 treatment、但不直接影响 outcome 的外生工具变量，恢复由 treatment 引起的那部分外生变化
- **识别假设**：相关性（instrument 影响 treatment）、排除性约束（instrument 不直接影响 outcome）、独立性（instrument 与潜在结果独立）；若解释为 LATE，还需单调性
- **估计方式**：先看 first stage，再做 reduced form 与 2SLS；弱工具变量时要考虑 weak-IV robust inference，而不是只报一个 2SLS 点估计
- **常见误区**：把 instrument 后产生的变量纳入 controls；把 first-stage 显著误当成排除性成立；工具很弱却继续解释系数；把 LATE 误写成总体 ATE
- **结果校验**：first-stage F 统计量只是最低门槛；检查 reduced-form 方向；过度识别检验只能辅助不能证明 exclusion restriction；需要回到机制上说明 instrument 为什么只通过 treatment 起作用

### 合成控制
- **方法定义**：用多个未处理单位的加权组合来构造处理单位的反事实轨迹
- **识别假设**：处理前轨迹能被 donor pool 充分拟合，且处理后没有其他只作用于处理单位的同步冲击
- **估计方式**：先优化 pre-period fit，再看 post-period gap；推断常靠 placebo / permutation inference
- **常见误区**：donor pool 混入受同类政策影响的单位；处理前拟合差却继续解释处理后 gap；只报平均 gap 不看时间路径
- **结果校验**：处理前 RMSPE、placebo 排名、leave-one-donor-out 稳健性

## 3. Refutation / 假设检验（用于结论强度）

高风险决策场景下应做假设检验；调用方只要求结构化输出时，主估计先按契约交付，未完成的检验只降低结论强度：

| 检验 | 做什么 | 预期结果 |
|------|--------|---------|
| Placebo 检验 | 用假 treatment | 效应应为零 |
| 子样本稳定性 | 在子集上复现 | 结果应一致 |
| 未观测混杂敏感度 | E-value / Rosenbaum bounds | 需要多强混杂才消除效应 |
| 随机噪声注入 | 加入随机变量做 treatment | 效应应不显著 |
| 随机共同原因 | 加入随机共同原因 | 效应估计应不变 |

**工具**：DoWhy refutation API (`refute_estimate()`)

**refutation 不通过**：降级结论强度或改用其他识别策略。不要忽略 refutation 结果，但也不要因此省略调用方要求的结构化主估计。

## 4. 练习案例 / 补充阅读

- DiD 练习时，先对照本文件写清处理组、对照组、时间变量、group 变量，再检查 pre-trend、placebo 时点和窗口稳健性
- IV 练习时，先写清 instrument 为什么只通过 treatment 起作用，再检查 first stage、reduced form 与 LATE 解释边界
- RDD 练习时，先写清 running variable 与 cutoff，再检查带宽敏感性、阈值附近操纵和协变量连续性
- 若方法选型仍不清晰，先回到 [30_causal_attribution_overview.md](30_causal_attribution_overview.md) 重写因果问句与识别假设

## 5. 各策略的实现级硬规则

本节只约束识别与主估计；稳健性检查不阻塞 [52](../core/52_output_guardrails.md) 交付。

### 5.1 DID 强制清单

- **从 §2a 的默认最小配方起步**：先判定是简单 2x2 还是多期/异步/非吸收/不平衡面板，再选择对应的最小等价实现。简单 2x2 用 `y ~ treat*post`；多期面板用 `y ~ treatment + group FE + time FE` 或题目/论文明确要求的等价 DiD 规格。任何偏离都要写明数据/识别上的具体触发条件（如 cohort ATT → CS/dCDH/Sun-Abraham；ATT 异质 → 事件研究）。
- 当存在面板组变量时，标准误**默认 cluster-by-group**；禁止用 OLS 默认 SE。处理组单位数 < 30 时可同时附 unit-level difference 的 Welch SE 作对账，但**不替代** cluster SE。
- **面板 FE 已能识别（单位/时间 FE 或与之等价）时，禁止默认追加 baseline 协变量**。看到基线不平衡先回到平行趋势讨论，而不是加控制；若确需加，控制变量必须先走 §5.4 的 bad-controls 预检，且只能是 pre-treatment。
- staggered adoption 不要无说明地套 TWFE；若目标是 cohort ATT，切换到 Callaway-Sant'Anna、de Chaisemartin-D'Haultfœuille 或 Sun-Abraham。若输出契约或原始研究定义的是当前 treatment 的 FE 系数，可以使用 TWFE，但必须说明口径。

### 5.2 IV / 2SLS / Encouragement 强制清单

- **从 §2a 的 IV 默认配方起步**：先算 first-stage、reduced-form、2SLS / Wald LATE，再讨论假设与偏离。
- 校验 `|λ̂ − β̂/α̂| / |λ̂| < 1%`。否则视为实现错误。
- first-stage F < 10（单工具）或 effective F < 23（多工具，Olea-Pflueger）→ 结论强度降级；弱工具时用 **Anderson-Rubin CI** 而非 t 区间，并标注 "弱工具变量警告"。
- LATE ≠ ITT ≠ ATE。估计量名词、主估计说明和结论必须保持一致；把 reduced-form 数值当成 LATE 汇报是严重错误。

### 5.3 RDD 强制清单

- **从 §2a 的 RDD 默认配方起步**（local linear + CCT/IK 单一带宽 + 空控制集）。
- **估计量命名**：sharp RDD 的估计量是**阈值处的条件平均处理效应（默认 CATE，即 X=cutoff 处的 ATE）**，不要写成 LATE/CLATE——sharp 设定下处理在 cutoff 确定性跳变，不存在“遵从者”子群体；LATE 是 fuzzy RDD / IV 的遵从者口径。只有 fuzzy RDD 才报 LATE。
- **主估计默认为局部线性 + CCT/IK 最优带宽**。全样本高阶多项式、二次/三次多项式仅作稳健性对照，**不能作为主估计**。
- **禁止主估计采用多带宽平均值**；主估计必须是单一带宽（默认 CCT 最优）下的点估计。
- fuzzy RDD 主估计必须基于 first-stage 与 reduced-form 的 Wald/IV-LATE。
- **fuzzy first-stage jump < 0.2 警告**：结论强度降级；标注“弱 first-stage，Wald 估计不稳定”，并补 Anderson-Rubin CI。“first-stage 在 0.10–0.20 × reduced-form 任意量级” 产生的大 LATE 不作为主结论。
- **量级闸门**：若 `|effect| > 5 × std(outcome)` 或超出 outcome 取值范围，触发 sanity 失败，结论降级为“待复核”。

### 5.4 Conditional Exogeneity（PSM / IPW / DML） 强制清单

- **从 §2a 的 CE 默认配方起步**；并优先核对：是否真的没有更弱假设的识别策略（RCT/DiD/IV/RDD）可走。
- 控制变量必须显式划分保留控制变量与 bad controls；对每个 bad control 写一句剔除理由。
- **bad-controls 预检**：对每个备选控制变量全部走一遍 post-treatment / mediator / collider / 与 treatment 机械相关四项检查，命中任一项入 bad controls。这一检查对所有 C 策略适用，不仅 PSM/IPW。在 DiD/IV/RDD 中若选择**空控制集**，则把「数据中可见的、可能被误用为控制」的变量写入 bad_controls 的责任仍然成立——不能因为没用就跳过这一预检。

### 5.5 输出与措辞的通用规则

本节不重复 52 的通用收敛护栏；本文件只保留各识别策略自己的实现级强制清单。

### 5.6 结果收口

输出边界见 [52](../core/52_output_guardrails.md)。
