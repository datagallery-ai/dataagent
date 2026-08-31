# Step2_2 清洗前置检查

> ⛔ **这是一个独立的 todo item，不可合并到 `step2_1` 或 `step2_3` 的 todo 中。**
>
> **已知复发故障**：0728-2 运行中 agent 从 step2_1 直接跳到 step2_3，step2_2 清洗逻辑完全跳过。
> 下游后果：step2_3 的"超前清洗"和"列表字段保护判断"都依赖 `step2_2_cleaning_report` 表——
> 表不存在 → step2_3 无法过滤 DROP 字段、无法保护列表字段。
>
> **执行顺序（严格）：**
> 1. `read_file("scripts/step2_2_cleaning_report.sql")` — 刷新清洗 SQL 模板记忆
> 2. submit cleaning_report.sql 建表 → 执行 cleaning_report 正确性门禁
> 3. 展开 `step2_2_feature_cleaning.sql` 的 `/*__CLEANING_EXCEPT_CLAUSE__*/` → submit 建表
> 4. 执行 `step2_2_validation.sql` 两条门禁
> 5. **全部通过后**才能标记此 todo 为完成，进入 step2_3

---

## 步骤 1：执行 cleaning_report.sql 建表

`submit_resource_job` 提交 `scripts/step2_2_cleaning_report.sql`。

> 模板是**单条 SELECT FROM `step2_0_column_profile`**，不包含 `/*__COLUMN_PROFILE_SELECTS__*/` 动态块。
> 如果 agent 自己改写了 SQL 加入了逐字段 UNION ALL + 硬编码 `'KEEP'` / `'DROP'` →
> **必须立即回退，使用模板的单条 CASE WHEN 写法。**

建表完成后立即执行正确性门禁：

```sql
SELECT feature, null_rate, recommendation
FROM {{output_database}}.step2_2_cleaning_report
WHERE recommendation = 'KEEP' AND null_rate > 50
```

- 返回行数 > 0 → **CASE WHEN 被绕过（硬编码 KEEP 字面量）**。检查 SQL 中是否有 `'KEEP' AS recommendation`，修正为 CASE WHEN 后重新建表
- 返回行数 == 0 → 通过

---

## 步骤 2：展开 `/*__CLEANING_EXCEPT_CLAUSE__*/` 提交 feature_cleaning.sql

**EXCEPT 展开流程：**

1. 查询 DROP 字段清单：
   ```sql
   SELECT feature FROM {{output_database}}.step2_2_cleaning_report WHERE recommendation = 'DROP'
   ```
2. 查询 step2_1 实际列清单：
   ```sql
   SELECT name FROM system.columns WHERE database = '{{output_database}}' AND table = 'step2_1_wide_simple'
   ```
3. **取交集**：EXCEPT 列表 = `{DROP 字段} ∩ {step2_1 列}`。禁止手工列出字段名
4. 若交集为空（DROP 字段都不在 step2_1 中，如常量已被 step2_1 显式排除）→ `EXCEPT` 子句可为空，SQL 语义为空但仍需 submit
5. 1:N 表的 DROP 字段不在本次 EXCEPT 范围——它们在 step2_3 聚合前由 cleaning_report 过滤

提交建表。

---

## 步骤 3：执行 validation.sql 两条门禁

第一条（完整性门禁）— 验证建表行数/列覆盖度：

```sql
-- 与 step2_2_validation.sql 第一条 SELECT 一致
SELECT count() AS n_rows, uniqExact(<user_id>) AS n_user_id, ...
FROM {{output_database}}.step2_2_wide_cleaned
```

第二条（正确性门禁）— 再次确认无高缺失率字段被 KEEP：

```sql
SELECT feature AS column_name, null_rate, recommendation,
       'ERROR: null_rate > 50 but KEEP — CASE WHEN bypassed' AS diagnosis
FROM {{output_database}}.step2_2_cleaning_report
WHERE recommendation = 'KEEP' AND null_rate > 50
```

若返回任何行 → **回退修正**。正确性门禁是兜底——如果步骤 1 的门禁被跳过，这里会再次捕捉。

---

## 步骤 4：标记完成，进入 step2_3

三条门禁（步骤 1 正确性 + 步骤 3 完整性 + 步骤 3 正确性）全部通过后 → 标记此 todo 完成 → 进入 step2_3。

step2_3 在 todo 0 前置检查中会再次 `SELECT count() FROM step2_2_cleaning_report` 确认表存在——这是二次兜底。

---

## 附录：清洗决策逻辑参考

| 条件 | 动作 | 理由 |
|------|------|------|
| `<user_id>`、`<label>`、`<age>`、`<gender>` | **保护** | 下游必需 |
| `sample_values` 含 `#` 或 `^`（列表字段） | **保护** | 空列表是有效信号，拆分为二元特征后值为 0，不应删除整列 |
| `missing_rate > 50`（且非列表字段） | 删除 | 普通字段缺失率过高 |
| `n_unique <= 1`（常量） | 删除 | 无信息量 |
| 其他 | 保留 | — |

> **列表字段保护规则**：`position(sample_values, '#') > 0 OR position(sample_values, '^') > 0` → 强制 `'KEEP'`。此条件排在 `missing_rate` 检查之前，确保列表字段即使 null_rate > 50% 也不会被误删。
> 反面案例：20260727-3 运行中 `game_interest_theme_u` (null_rate=65%)、`social_interest_strange_social_app_u` (95.9%) 等 5 个列表字段被 DROP，step2_3 无法对其拆分。

## 反面案例速查

| 字段 | step2_0 missing_rate | 正确 recommendation |
|------|---------------------|-------------------|
| social_interest_strange_social_app_u | 95.91% | DROP（非列表字段） |
| fun_fact_sport_match_u | ~99% | DROP |
| game_interest_theme_u | 64.98% → 但 sample_values 含 `#` | **KEEP（列表字段保护）** |
