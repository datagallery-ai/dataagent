# C.1 实验方法（RCT 分析）

## 1. 适用前提

有随机分组的实验数据（A/B Test、随机对照试验），且随机化在分析样本中未被破坏。随机化只有在未被破坏时才保证可忽略性成立；若存在差异性流失、系统性不合规或明显的协变量不平衡，应回到 [30 §1.5](30_causal_attribution_overview.md) 改走 conditional exogeneity，而不是默认套 RCT 均值差。

### 识别假设

- **随机分配**：分组在处理前生成，且不被运营、用户行为或后续系统逻辑篡改
- **SUTVA / 无干扰**：一个用户是否在 treatment，不应直接改变另一个用户的潜在结果；社交传播、竞价挤压、库存共享场景要特别警惕
- **无差异流失**：treatment 和 control 不能因为处理本身产生系统性 attrition / missingness
- **可达性与重叠**：两组都要有实际被观察和被测量的机会；没有曝光资格的人不能被当作有效 treatment 样本

## 2. 默认流程

### Step 1. 验证随机化质量（不可跳过）

1. **协变量平衡检查**：
   - 对每个 pre-treatment 协变量，比较 treatment 和 control 分布
   - 标准化均值差 SMD < 0.1 为良好，0.1-0.25 可接受，> 0.25 需排查
   - 连续变量用 t-test/KS-test，分类变量用 chi-square

2. **分组比例检查**：实际比例是否与预设一致
   - 显著偏离 → 检查分组逻辑 bug（Sample Ratio Mismatch）

3. **曝光资格检查**：是否所有 treatment 用户都有机会被处理

4. **合规性检查**：实际 treatment 是否按分组执行
   - 有不合规 → 用 ITT 分析或 IV 方法

### Step 2. 选择估计量

| 估计量 | 含义 | 何时用 |
|--------|------|--------|
| ATE | 平均处理效应 | 关心总体效果 |
| ATT | 处理组平均效应 | 关心实际接受处理者 |
| ITT | 意向性处理效应 | 有不合规时的保守估计 |

**选择规则**：
- 完全合规 → ATE = ITT，直接用均值差
- 有不合规 → 先报 ITT（保守），再用 CACE/LATE

### Step 3. 估计效应与置信区间

- 连续结果：均值差 + 回归估计（可用 CUPED 减方差）
- 二值结果：转化率差 + 标准误 + CI

**常用估计方式**：
- 个体随机实验：均值差或只含 treatment 指示变量的 OLS，标准误用 heteroskedasticity-robust
- 分层随机实验：在估计中加入 strata 固定效应，或按 strata 单独估计后加权汇总
- cluster 随机实验：按 cluster 分析，或至少使用 cluster-robust standard errors；不能把 cluster 实验当独立用户实验来报 SE
- 有不合规：主结果先报 ITT；若要估计接受处理者效应，用 assignment 作为 instrument 估 CACE / LATE

**方差缩减技术**：
- CUPED：用 pre-experiment 指标做协变量
- 分层随机化 + 分层估计

### Step 4. 分层分析（可选）

- 按预设分群看效应异质性
- 事后分层需做多重检验校正（Bonferroni / FDR）
- 如需系统化 HTE → 转 [33_heterogeneous_effects.md](33_heterogeneous_effects.md)

## 3. 结果校验

- **主估计与调整后估计方向应一致**：raw difference、回归调整、CUPED 结果方向若明显冲突，优先排查随机化、口径或缺失问题
- 实验健康度（样本量、基线率、attrition、SRM、实验时长、CI）只作诊断补充；只报 uplift 不做健康度检查时，结论强度降级
- **长期指标看动态路径**：短期 uplift 不代表长期增量，至少看按时间展开的 treatment effect 曲线
- **多指标要分主次**：先声明 primary metric，再处理 secondary metrics 的多重检验

## 4. 常见陷阱

| 陷阱 | 后果 | 防范 |
|------|------|------|
| 按实际曝光分析（非 ITT） | 选择偏差 | 按分配组分析 |
| Peeking（早停偷看） | 假阳性膨胀 | 序贯检验或预设 peek 计划 |
| 新奇效应 | 高估长期效应 | 看长期趋势 |
| SUTVA 违反（用户间干扰） | 效应有偏 | cluster 随机化 |
| 多重检验 | 假阳性 | Bonferroni / BH |
| SRM（样本比例不匹配） | 分组不平衡 | 检查 SRM |
| 控制了 post-treatment 变量（如实际曝光、实验后活跃度） | 摘走真实效应或引入偏差 | 只控制 pre-treatment 协变量 |
| 忽略 attrition / 缺失差异 | 随机化被破坏 | 报 attrition 差异并做稳健性分析 |
| cluster 实验按用户独立报标准误 | 标准误过小，虚假显著 | 用 cluster 级分析或 cluster-robust SE |

## 5. 练习案例 / 补充阅读

- 练习 RCT 节点时，优先检查三件事：estimand 是否写清、bad controls 是否排除、ITT 与 CACE 是否区分
- 若实验存在不合规、干扰或 cluster 随机化，交付前回到本文件重新核对识别假设、估计方式与标准误口径
- 若需要进一步做分层或个体差异分析，再进入 [33_heterogeneous_effects.md](33_heterogeneous_effects.md)

## 6. 输出收口

RCT 任务按调用方契约输出；本文件只负责实验设计、估计量选择和实验健康度检查。结构化输出边界见 [52_output_guardrails.md](../core/52_output_guardrails.md)。
