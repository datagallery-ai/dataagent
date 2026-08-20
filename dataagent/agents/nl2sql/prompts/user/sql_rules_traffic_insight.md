# SQL Generation Rules

## Step 0: Classify the Query Type (MANDATORY - Apply First)

Classify into exactly one type, then apply only that type’s rules:

- **Type A (Metric query)**: Single-period metrics (traffic, rate, ratio, share / success rate, counts, TopN, aggregation, direct value). Includes same-period dimension comparisons. Does **not** compare two time periods.
- **Type B (Period comparison)**: Compares two different time periods (同比 / 环比 / 与某时段对比). Bare 对比 is not enough unless the compared items are time periods.

**Question target priority:** If the question explicitly names comparison period, output fields/aliases, grouping dimensions, or metric formula, follow that wording over generic keyword defaults below.

Within **Type A**, pick one pattern:

| Pattern | SQL focus |
|---------|-----------|
| Direct value | Object + time window → one aggregated row; no `time` in GROUP BY |
| Aggregation | GROUP BY involved dims; add `time` only for trend / per-bucket output |
| Ranking / TopN | GROUP BY rank dim + `ORDER BY metric DESC/ASC` + `LIMIT N` |
| Derived metric | Section 1.3 / 1.4 formulas |

## 1. Type A: Metric Query

### 1.1 Common Rules

- Descriptions starting with `维度` / `指标` are dimensions / metrics.
- A dimension is involved only when the question refers to it or one of its values. SELECT / GROUP BY = exactly those dimensions; metrics use `SUM()` (or schema aggregate) and stay out of GROUP BY.
- **Literal priority** (first match wins): (1) question `key=<integer>` → unquoted int; (2) column `example:` style; (3) else INTEGER/BIGINT unquoted, TEXT quoted. Never quote INTEGER/BIGINT.
- WHERE = dimension filters + time window; HAVING = aggregated metric thresholds. Put a dimension in WHERE only when other values must be excluded from the **entire** result.
- 「不按 X 过滤」「汇聚全部 X」: do not filter on X; `SUM()` across all X.
- Table already matches requested grain. Use bare `time` when it appears in SELECT / GROUP BY / ORDER BY.
- **「粒度…」** (粒度5min / 粒度1h …) only selects table grain — it does **not** imply per-bucket output or `GROUP BY time`. Include `time` in SELECT / GROUP BY only for 时间序列 / 趋势 / 每小时 / 按时间点 / per-bucket wording. A time **range** or bare 「粒度…」 alone does not.
- Division: `::numeric` + `NULLIF` on denominators. Multiply by 100 only when percent is required. Prefer column `relation_formula` when present.

### 1.2 Pattern Notes

**Direct value:** filters + window in WHERE; one row (or one per involved non-time dim); omit `time` unless series/trend asked. Bare 「粒度…」 stays Direct value.

**`cell` (BIGINT):** strip display-only hex suffix (`B`/`H`) or `0x` prefix; remaining digits as unquoted int; no base conversion.

**Aggregation / TopN:** GROUP BY involved (rank) dims; add `time` only for per-bucket/trend; TopN uses `ORDER BY` + `LIMIT N`.

### 1.3 Derived Metric Formulas

`time` = Unix epoch seconds (bucket start). **`business_time_window_seconds`** = rate divisor (业务时间窗), separate from `time`.

**Traffic volume** (流量): `SUM(uplink_volume)` / `SUM(downlink_volume)` / `SUM(uplink_volume) + SUM(downlink_volume)`. If question says NULL 按 0: `SUM(COALESCE(uplink_volume, 0) + COALESCE(downlink_volume, 0))`.

**Bandwidth / rate** (带宽 / 上下行速率):

- Total: `(SUM(uplink_volume::numeric) + SUM(downlink_volume::numeric)) / NULLIF(business_time_window_seconds, 0)`
- Uplink / Downlink: `SUM(<col>::numeric) / NULLIF(business_time_window_seconds, 0)`

`business_time_window_seconds`: whole-period → query window seconds（最近5分钟=`300`，1小时=`3600`，1天=`86400`）；per-bucket → table grain（`5min`=`300`，`1h`=`3600`，`1d`=`86400`，`1w`=`604800`，`1m`=`2592000`）.

**Traffic ratio:** `SUM(uplink_volume::numeric) / NULLIF(SUM(downlink_volume::numeric), 0)`

**Counts:** `SUM(connections)`, `SUM(subs_count)`

### 1.4 Share / Success-Rate (占比 / 成功率 / 命中率)

When schema has no dedicated success-rate column and the question asks for 占比 / 成功率 / 命中率 / `(subset/total)*100%`:

- Numerator = metric on named subset; denominator = **same** metric over **all** values of that splitting dim at the **same grain** (unless question restricts denominator). Literals via §1.1.
- Do **not** put the subset dim in WHERE. No window / no `WITH`. Per-bucket: both sides in the same `GROUP BY` (no whole-window scalar denominator).

```text
SUM(CASE WHEN <dim> = <subset_literal> THEN <metric> ELSE 0 END)::numeric
  / NULLIF(SUM(<metric>), 0)   -- ×100 only if percent required
```

```sql
SELECT <dims_incl_time_if_needed>,
  (SUM(CASE WHEN <dim> = <subset_literal> THEN <metric> ELSE 0 END)::numeric
   / NULLIF(SUM(<metric>), 0)) * <100_if_percent_else_omit> AS <alias>
FROM <table>
WHERE <time_and_non_subset_filters>
GROUP BY <dims_incl_time_if_needed>
ORDER BY <time_if_in_select>
```

## 2. Type B: Period Comparison

- Resolve periods from the **question’s named target** (Section 3) first — e.g. 「上周同一天同比」→ week offset, **not** 「同比去年」.
- Build bounds with `INTERVAL` on timestamps, then `EXTRACT(EPOCH FROM ...)::bigint`. Half-open filters; never subtract offset from an extracted bigint.
- No `WITH` / CTE. Nested subqueries in `FROM`: `CROSS JOIN` if no join dims; otherwise join on involved non-time dims. Same filters + same `<metric_expression>` in both sides; `SUM()` across non-involved dims.
- Own alias per subquery; never read `comparison_value` from the current alias (or reverse).
- Output: fields the question asks for (often dims + `current_value` + `comparison_value` + `change_rate`). Plain 对比 without rate wording → values only. Derived metrics: same §1.3 / 1.4 in both periods.

### 2.1 Whole-period vs per-bucket

- **Whole-period** (default for 今天 vs 上周同一天, etc.): `time` only in WHERE — not in SELECT / GROUP BY / JOIN. 「粒度…」 alone does not force `GROUP BY time`.
- **Per-bucket:** only if question asks multi-bucket breakdown inside each period.

### 2.2 Formulas

```text
change_rate = (current_value::numeric - comparison_value::numeric)
              / NULLIF(comparison_value::numeric, 0)

current_time_filter:
  time >= EXTRACT(EPOCH FROM current_start_timestamp)::bigint
  time <  EXTRACT(EPOCH FROM current_end_timestamp)::bigint

comparison_time_filter:
  time >= EXTRACT(EPOCH FROM (current_start_timestamp - offset))::bigint
  time <  EXTRACT(EPOCH FROM (current_end_timestamp - offset))::bigint
```

### 2.3 Whole-period SQL templates (no WITH)

No join dims:

```sql
SELECT cur.current_value, cmp.comparison_value,
  (cur.current_value::numeric - cmp.comparison_value::numeric)
    / NULLIF(cmp.comparison_value::numeric, 0) AS change_rate
FROM (
  SELECT <metric_expression> AS current_value FROM <table>
  WHERE <dimension_filters> AND <current_time_filter>
) AS cur
CROSS JOIN (
  SELECT <metric_expression> AS comparison_value FROM <table>
  WHERE <dimension_filters> AND <comparison_time_filter>
) AS cmp
```

With join dims (never `CROSS JOIN` multi-row period sides that must align):

```sql
SELECT cur.<dim>, cur.current_value, cmp.comparison_value,
  (cur.current_value::numeric - cmp.comparison_value::numeric)
    / NULLIF(cmp.comparison_value::numeric, 0) AS change_rate
FROM (
  SELECT <dim>, <metric_expression> AS current_value FROM <table>
  WHERE <current_time_filter> GROUP BY <dim>
) AS cur
FULL OUTER JOIN (
  SELECT <dim>, <metric_expression> AS comparison_value FROM <table>
  WHERE <comparison_time_filter> GROUP BY <dim>
) AS cmp ON cur.<dim> = cmp.<dim>
```

## 3. Time Window Rules

- `time` = Unix epoch seconds (bucket start). Filters half-open. Boundaries via `EXTRACT(EPOCH FROM ...)::bigint`.
- Relative words (今天 / 本周 / 本月 / 最近 N …) anchor on `NOW()`. 「现在为…」 is calendar context only — do not replace `NOW()` with a fixed date.

| Window | Bounds |
|--------|--------|
| 今天 | `date_trunc('day', NOW())` → `NOW()` |
| 本周 / 这周 | `date_trunc('week', NOW())` → `NOW()` |
| 本月 | `date_trunc('month', NOW())` → `NOW()` |
| 最近 N … | `NOW() - INTERVAL '…'` → `NOW()` |
| 昨天 | `[date_trunc('day', NOW()) - 1 day, date_trunc('day', NOW()))` |
| 上周 | `[date_trunc('week', NOW()) - 1 week, date_trunc('week', NOW()))` |
| 上个月 | `[date_trunc('month', NOW()) - 1 month, date_trunc('month', NOW()))` |

Absolute date/time: half-open; omit year → current year; `timestamptz` then epoch.

**Comparison offsets** (named target first; shift current bounds):

- 今天环比昨天 / 昨天环比前天: `INTERVAL '1 day'`
- 上周同一天 / 同比上周 / 本周环比上周 / 与上周同一天同比: `INTERVAL '7 days'`（**not** `1 year`）
- 本月环比上月 / 同比上月: `INTERVAL '1 month'`
- 最近 N 小时/天环比: `INTERVAL 'N hours|days'`
- 同比去年 / 去年同期 / bare 同比 with no period named: `INTERVAL '1 year'`

## 4. General Rules

- Only tables/columns in Database Schema. Literals: §1.1. Cell hex markers: §1.2.
- PostgreSQL; no backticks; ASCII aliases; `IS NULL` / `IS NOT NULL` only for null predicates.
- **No** window functions (`OVER (...)`); **no** `WITH` (CTE) — use nested `SELECT` / join of subqueries.
- Output aliases are projections only. Advanced functions only if schema/evidence confirms them.

# Output

Respond with ONLY the SQL query, enclosed in a ```sql``` block.
