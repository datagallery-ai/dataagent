# Step2_3 残留 String 列转换（采样后 CAST）

> ⛔ **独立 todo，不可合并到列表分割或建表 submit。** 必须在 `step2_3_format_gate.md` 通过之后、submit `step2_3_wide_complete` **之前**完成。
> CAST 写进同一条 `CREATE OR REPLACE TABLE step2_3_wide_complete` 的最外层 SELECT（对 `w.*` 中待转换列 `EXCEPT` 后再写表达式）。禁止 `ALTER`，禁止 `parseDateTimeBestEffort` 未采样就提交。
>
> **已知复发故障**：宽表留下数十个 `String`/`Nullable(String)`，其中高基数（n_unique>50）如 `funny_time`、`city`、时长/版本字段导致 CSV 膨胀、无法直接入模。根因：SKILL 无转换规则；Agent 未采样就试 `parseDateTimeBestEffort`；中断后不知道做到哪一步。

保护列不转换：`<user_id>`、`<label>`、`<age>`、`<gender>`。
已按 `#`/`^` 做 splitByChar 二元展开的列表字段不走本规则（原列应已 EXCEPT 掉）。

---

## 转换规则（按采样结果，优先级从上到下，命中即停）

| 采样判定 | 动作 | 产出列 |
|----------|------|--------|
| 列名像城市（`city` / `city_name` / `*城市*`，排除 cityHash） | **城市分级** | **删除原列**（过拟合：宽表禁止 `city` 原列）；用 `scripts/step2_3_city_tier_sql.py` 生成 `CAST(multiIf(...)) AS {col}_tier`，取值 `一线` / `新一线` / `二线` / `三线` / `三线及以下` / `未知` / NULL |
| ≥95% 非空样本匹配 `^-?[0-9]+(\.[0-9]+)?$` | 转数值 | `toFloat64OrNull(trimBoth(toString(col))) AS col` |
| ≥50% 非空样本含 `,` 且不是纯数字 | 逗号列表 | 删除原列；`length(splitByChar(',', assumeNotNull(col))) AS col_list_length` + `(col IS NULL OR toString(col) = '') AS col_is_empty` |
| 其他（版本号、混杂文本） | 低基数编码 | `CAST(col AS LowCardinality(String)) AS col` |

- 城市分级映射见 `references/city_tier_map.json`（第一财经分级表：一线北上广深；新一线含昆明；二线 30 城含无锡）。**只看市、不看县**：`北京市` / `北京 市` / `北京` 都按市匹配；以 `县` / `自治县` 结尾的地名不升到所属地级市，一律 `三线及以下`。未入榜的市 → `三线及以下`；`未知`/`未知市` → `未知`；空值 → NULL。
- **禁止**把城市列只做成 `LowCardinality(String)` 原样保留高基数地名。
- **禁止过拟合**：`step2_3_wide_complete` 与最终 CSV **不得出现** `city` / `city_name` / `*城市*` 原列，只保留 `{col}_tier`。`w.* EXCEPT (city)` 后写 `city_tier`。`step2_3_validation.sql` 第三条查询命中原列 → 门禁失败。
- **禁止**未采样使用 `parseDateTimeBestEffort` / `toDate` / `toDateTime`。仅当 200 条样本 **全部** 能被 `parseDateTimeBestEffortOrNull` 解析为非 NULL 时才允许转时间类型，否则走 LowCardinality(String) 或纯数字规则。
- 空串与 NULL 保持缺失（`toFloat64OrNull` / 空串 → NULL），不要填 0。

---

## 步骤 1：列出待转换列

从 `step2_2_wide_cleaned` 的 `system.columns` 取 `type` 含 `String` 且不是 `LowCardinality` 的列，排除保护列和 `list_fields.txt` 中已分割字段。

1:N 聚合 CTE 新产出的 String 列一并列入（对照 expanded SQL 的 CTE SELECT 别名）。

---

## 步骤 2：转换前必须采样（每列一条 SELECT）

禁止凭列名猜测。对每个候选列用 MCP `submit_resource_job`：

```sql
SELECT
    count() AS n_sample,
    uniqExact(val) AS n_unique_sample,
    countIf(match(trimBoth(val), '^[-+]?[0-9]+(\\.[0-9]+)?$')) AS n_numeric,
    countIf(position(val, ',') > 0) AS n_has_comma,
    countIf(parseDateTimeBestEffortOrNull(val) IS NOT NULL) AS n_datetime
FROM
(
    SELECT toString(assumeNotNull({col})) AS val
    FROM {{output_database}}.step2_2_wide_cleaned
    WHERE {col} IS NOT NULL AND toString({col}) != ''
    LIMIT 200
)
```

1:N 源表字段把 `FROM` 换成原始源表。把每列的判定写入 `string_cast_plan.txt`（每行：`列名 规则 numeric|city_tier|comma_list|lowcard 比例`）。

城市列在采样后仍走 `city_tier`（列名匹配即生效，不要求 95% 数字）。生成表达式：

```bash
python skill/feature-engineer/scripts/step2_3_city_tier_sql.py --column {col} --table-alias w
```

将 stdout 整段贴进最外层 SELECT，并把 `{col}` 加入 `EXCEPT`。expanded SQL 必须同时满足：含 `{col}_tier`、不再 SELECT 原始 `{col}`（保护列除外）。建表后若 `system.columns` 仍有 `city` 原列 → 失败（过拟合）。

任一列未采样 → **禁止 submit 建表**。

---

## 步骤 3：把规则写进最外层 SELECT

```sql
SELECT
    w.* EXCEPT (funny_time, game_installed_app_30d, city)
    , toFloat64OrNull(trimBoth(toString(w.funny_time))) AS funny_time
    , length(splitByChar(',', assumeNotNull(w.game_installed_app_30d))) AS game_installed_app_30d_list_length
    , (w.game_installed_app_30d IS NULL OR toString(w.game_installed_app_30d) = '') AS game_installed_app_30d_is_empty
    , CAST(
        multiIf(
        w.city IS NULL OR trimBoth(toString(w.city)) = '', NULL,
        match(replaceAll(replaceAll(trimBoth(toString(w.city)), ' ', ''), '　', ''), '(自治县|县)$'), '三线及以下',
        /* 去「市」后匹配一线/新一线/二线/三线；县名已在上一支截住，禁止升到地级市 */
        '三线及以下'
        ) AS LowCardinality(String)
    ) AS city_tier
    /* city_tier 完整表达式必须由 step2_3_city_tier_sql.py 生成：兼容 北京市/北京 市，只看市不看县 */
```

列名以采样计划为准，上表仅为格式。完成后重跑 `step2_3_check_join_nesting.py`（CAST 不得引入扁平多 JOIN）。

---

## 步骤 4：进度标记

本 todo 完成后追加 `progress_step2_3.txt`（见 SKILL.md「进度标记」）。建表 + 高基数门禁通过后再更新为 `todo=5 status=done`。
