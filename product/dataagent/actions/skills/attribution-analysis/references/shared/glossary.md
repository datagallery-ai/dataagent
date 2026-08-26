# 扩展术语表

本文件收录跨文件复用的长尾术语。

## 因果与识别（C 分支补充）

| 缩写/术语 | 中文含义 | 英文全称 | 白话解释 + 误用提醒 |
|---|---|---|---|
| SUTVA | 稳定单元处理值假设：某单元的潜在结果不因他人是否接受处理而改变（无严重溢出/P2P 干扰时常用） | Stable Unit Treatment Value Assumption | 可理解为“互不干扰”；若有串扰/外溢，因果效应会偏。 |
| AR CI | Anderson-Rubin 置信区间：弱第一阶段时仍可用的结构参数区间 | Anderson-Rubin confidence interval | 工具变量较弱时优先看这个区间；别只看普通 t 区间。 |
| SMD | 标准化均值差：用于检查处理组/对照组协变量平衡或匹配加权后的平衡 | Standardized Mean Difference | 用来判断两组是否可比；显著性检验通过不代表平衡就足够。 |
| GT | 基准真值：仿真、半合成或审计对照中的参考真值 | ground truth | 就是“拿来对答案的真值”；没有 GT 时别把误差指标说得过硬。 |
| CCT / IK | RDD 常用带宽选择规则；主估计仍需回到 [32](../c/32_quasi_experimental_methods.md) 的 RDD 强制清单 | Calonico-Cattaneo-Titiunik / Imbens-Kalyanaraman | 帮你选 RDD 带宽；别把多个带宽平均当主结果。 |
| CATE（阈值处） | 条件平均处理效应；在 RDD 中指 **X=cutoff 处的 ATE**，是 sharp RDD 的目标估计量 | Conditional Average Treatment Effect (at the cutoff) | sharp RDD 估的是“恰好在阈值上那群人的平均效应”；它**不是** LATE——sharp 设定下不存在“遵从者”子群体。只有 fuzzy RDD / IV 才用遵从者口径的 LATE。 |

约化式、聚类标准误、bad controls 等实现细节见 [32_quasi_experimental_methods.md](../c/32_quasi_experimental_methods.md)。
