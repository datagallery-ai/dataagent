# Step2_2 残留 String 列转换（采样后 CAST）

> ⛔ 采样 SQL 与判定都脚本化：`prepare` 后按 `step2_2_run.json` 的 `after_collect` 顺序执行
> （`sample` → 提交采样 → `string-plan` → 带 `--string-plan` 重跑 `finalize`），命令形参由脚本写死在清单里。
> 禁止手工改写 expanded SQL、禁止手写采样 SQL。
>
> 转换表达式由 `step2_2_expand_sql.py` 读取 `references/aggregation_policy.json` 的
> `string_cast_rules` 生成；判定阈值读 `string_numeric_ratio` / `string_comma_ratio`。
>
> 保护列不转换：`<user_id>`、`<label>`；维度表的字符串列保留为 LowCardinality(String)。
> 已按 `#`/`^` 做 splitByChar 二元展开的列表字段不走本规则（原列已 EXCEPT）。

---

## 两类候选列

`sample` 枚举出全部候选并列在 `step2_2_string_candidates.json`，分两类：

| 类别 | 来源 | 是否必须转换 |
|------|------|-------------|
| `required` | 1:N 聚合 CTE 产出的 String 列（`max(String)`） | **必须**——不在 validation 放行清单内，残留即触发高基数门禁（2k 会拦） |
| `optional` | step2_1 拍平后原样透传的物理 String 列 | 可选——validation 放行清单允许保留 `String` |

采样直接读**源表原始列**（1:N 派生列取自其所属 1:N 源表），不需要先建宽表。

## 转换规则

规则名 → 动作的映射、判定阈值（numeric 比例、comma 比例）全部只写在
`references/aggregation_policy.json` 的 `string_cast_rules` 与 `string_numeric_ratio` /
`string_comma_ratio`，**不得在别处另写数值**。`string-plan` 会逐列打印实际判定
（`列 -> 规则（样本 N，numeric …，comma …）`）并写入 `string_cast_plan.txt`，不要自行判定。

判定优先级（命中即停）：列名匹配 `geo_columns` → `city_tier`；nearly-all 数字 → `numeric`；
含逗号比例超阈值 → `comma_list`；全部可解析为时间 → `datetime`；其他 → `lowcard`。

- 城市分级映射见 `skill/feature-engineer/references/city_tier_map.json`（第一财经分级表）。**只看市、不看县**：`北京市` / `北京 市` / `北京` 都按市匹配；以 `县` / `自治县` 结尾的地名不升到所属地级市，一律 `三线及以下`。未入榜的市 → `三线及以下`；`未知`/`未知市` → `未知`；空值 → NULL。完整表达式由 `skill/feature-engineer/scripts/step2_2_city_tier_sql.py` 生成。
- **禁止**把城市列只做成 `LowCardinality(String)` 原样保留高基数地名。
- **禁止**宽表与最终 CSV 出现地理原列（模式见 `references/scene.yaml` 的 `geo_columns`），只保留 `{col}_tier`（维度表的 CROSS JOIN 层同样会 scrub）。`step2_2_validation.sql` 第三条查询命中原列 → 门禁失败。
- **禁止**在未采样时使用 `parseDateTimeBestEffort` / `toDate` / `toDateTime`，只允许 `parseDateTimeBestEffortOrNull`。
- 空串与 NULL 保持缺失（`toFloat64OrNull` → NULL；`comma_list` 只产出长度/空标记），不要填 0。

---

## 落点

`required` 列的 CAST 写在外层：1:N 聚合 CTE 内必须是 `max(<alias>.<col>)`（2f 门禁要求字面量），
转换只能放在最外层投影，由 `EXCEPT` 掉 CTE 产出的原列后重写。`optional` 列同理走最外层。

`string_cast_plan.txt` 是**判定记录**，每行 `列名 规则`；`sample` → `string-plan` 自动生成，
如需人工改判直接编辑该文件后重跑 `finalize` 即可（脚本不覆盖已有判定之外的内容）。

若 `sample` 报「没有需要采样的候选列」，说明宽表没有 String 风险，清单的 `after_collect` 会直接跳 `finalize`（不带 `--string-plan`）。
