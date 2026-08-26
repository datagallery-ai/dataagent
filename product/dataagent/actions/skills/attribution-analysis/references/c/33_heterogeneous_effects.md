# C.3 异质效应（HTE/CATE）

## 1. 何时需要

- 不满足于平均效应，需要"对哪些人群效果更大/更小"
- 需要做差异化策略（不同人群不同干预）
- 平均效应不显著但怀疑正负抵消

### 识别前提

- **HTE 不是跳过主效应识别的捷径**：如果 ATE 的识别站不住，CATE 也站不住
- **分群变量必须是 pre-treatment**：不能拿处理后的行为、曝光、留存、打开率去切分“谁更有效”
- **观测数据做 HTE**：除了主设计本身可识别，还要保证各子群内仍有重叠性；没有 overlap 的 subgroup 不应硬报 CATE
- **发现与估计最好分离**：用 honest splitting / cross-fitting 降低把噪声当异质性的风险

## 2. 方法选择

| 方法 | 核心思路 | 适用条件 | 优点 | 局限 |
|------|--------|---------|------|------|
| 预设分层 | 按先验维度分组 | 结构简单 | 直观 | 只能看预设维度 |
| Causal Forest | 非参数异质效应 | 数据量 > 5000 | 自动发现异质性 | 黑盒 |
| Meta-learners | ML 估计 CATE | 灵活 | 可定制 | S-learner 可能正则化掉效应 |
| DML | 正交化 + ML | 高维混杂 | 理论保证 | 需交叉拟合 |
| Uplift modeling | 直接建模 uplift | 有 RCT 数据 | 直接优化 | 需 RCT |

### 2.1 方法选择决策规则（按 N × 协变量维度）

选哪种 CATE 估计器不能凭习惯，应按数据规模与协变量结构决定。**默认优先选最弱的可行方案**，避免“小样本 + 高复杂度模型”制造伪异质性。

| 样本量 N | 候选维度数 / 是否高维 | 推荐方法 | 理由 |
|---|---|---|---|
| < 1000 | 少量已知分群维度 | 预设分层 + 交互检验（带多重检验校正） | 数据不足以支持非参 CATE |
| 1000 – 5000 | 少量已知分群维度 | 预设分层 + Meta-learner（T / X / DR） | 可在已知分群上跑 ML CATE |
| 1000 – 5000 | 高维 / 未知异质来源 | DML + 简单基学习器 | 高维混杂控制优先于 CATE 自动发现 |
| > 5000 | 中等维度 | Causal Forest 或 DR-learner | 可承担非参异质性发现 |
| > 5000 | 高维（≥ 数十维）| DML + Causal Forest，或 GRF | 兼顾混杂控制与异质性发现 |
| 任意，且 RCT | 任意 | Uplift modeling / Policy learning | RCT 下 uplift 可直接优化决策 |

**硬规则**：

1. CATE 主结论应能追溯到：N、子群最小样本量、协变量维度、是否使用 honest splitting / cross-fitting
2. 子群样本量 < 30，或子群内 propensity overlap 不充分（任一处理水平的占比 < 5%）→ **不得**为该子群单独输出 CATE 结论；合并或标注“样本不足”
3. 用预设分层时，诊断补充可给出组间交互检验的 p（带多重检验校正），不得只报每组的组内 p
4. CATE 排序的主评价指标用 **AUUC / Qini / policy value**，而不是“某子群 p < 0.05”；并要求在留出集上评估
5. 若主 ATE 在原始识别策略下不显著，CATE 结论必须改写为“探索性 / 待复核”，不得作为决策依据

### 预设分层分析
- 按先验维度分组，每组内独立估计 ATE
- 做交互检验判断组间差异显著性
- **注意**：多重检验校正

### Causal Forest
```python
from econml.dml import CausalForestDML
est = CausalForestDML(model_y=..., model_t=...)
est.fit(Y, T, X=X, W=W)
cate = est.effect(X_test)
ci = est.effect_interval(X_test, alpha=0.05)
```

### Meta-Learners
- S-Learner：单模型，T 作特征（简单但可能正则化掉效应）
- T-Learner：分别对 T=0 和 T=1 建模（直观但不共享信息）
- X-Learner：T-Learner + cross-estimation（小处理组时好）
- DR-Learner：双重稳健（理论保证最好）

## 3. 评估方法

反事实不可观测，无法逐个验证 CATE。需要专门指标：

| 指标 | 含义 | 说明 |
|------|------|------|
| AUUC | uplift 曲线下面积 | 按 CATE 降序排列，画累积 uplift |
| Qini 系数 | uplift 排序质量 | 类似 Gini |
| Policy Value | 按 CATE 分配后的总增量 | 直接衡量决策价值 |
| PEHE | 个体效应恢复误差 | 需要真值（半合成数据） |

**不能用普通 AUC 作为主评估**——AUC 评估预测精度，不是 uplift 排序。

补充要求：
- **优先看排序是否有用**：如果最终用于投放或资源分配，AUUC / Qini / policy value 比“某个 subgroup 的 p-value”更重要
- **必须在留出集上评估**：训练集里的异质性很容易只是噪声拟合
- **不确定性诊断**：可给重点 subgroup 的区间，或对分箱后的 uplift 做稳定性检查

## 4. 常见误区

- 先用全量数据找“高 uplift 人群”，再在同一批数据上宣称该分群有效
- 把叶子节点、深层分箱里的小样本符号翻转当成业务规律
- 把 CATE 解释成“因果机制已经确定”，而不是“在当前设计下可用于排序/分配的条件效应” 
- 用普通分类指标替代 uplift / policy value 指标

## 5. 结果校验

- **校验排序稳定性**：重抽样后 top decile / top quintile 人群是否稳定
- **校验部署可行性**：模型识别出的 subgroup 是否真能在策略系统中被稳定触达
- **校验收益而非只校验显著性**：若高 uplift 人群样本太小、不可执行，业务价值依然有限
- **与主设计对齐**：RCT 上的 HTE 重点看 policy value；观测数据上的 HTE 必须回到主识别策略的敏感性分析

## 6. 工具

- EconML：CausalForestDML, Meta-Learners, DRTester
- CausalML：UpliftRandomForest, UpliftTree
- scikit-uplift：uplift modeling 工具

## 7. 练习案例 / 补充阅读

- 若输出口径是 CATE / CATT / CLATE，应先回到主设计确认识别条件，再进入异质效应估计
- 若异质性结论将用于资源分配，交付前至少复核留出集评估、policy value 和 subgroup overlap

## 8. 输出收口

HTE 任务按调用方契约输出；本文件只补充异质效应的识别前提、方法选择和评估口径。结构化输出边界见 [52_output_guardrails.md](../core/52_output_guardrails.md)。
