# SQL Generation Rules

## Step 0: Classify the Query Type (MANDATORY - Apply First)

Classify into exactly one type, then apply only that type’s rules:

- **Type A (Metric query)**: Single-period metrics (traffic, rate, ratio, share / success rate, counts, TopN, aggregation, direct value). Includes same-period dimension comparisons. Does **not** compare two time periods.
- **Type B (Period comparison)**: Compares two different time periods (同比 / 环比 / 与某时段对比). Bare 对比 is not enough unless the compared items are time periods.

**Question target priority:** If the question explicitly names comparison period, output fields/aliases, grouping dimensions, or metric formula, follow that wording over generic keyword defaults below. **Exception — aggregation is mandatory:** a question formula names **columns and algebra only**; it is **not** paste-ready SQL. Rewrite every metric identifier with `SUM(...)` per §1.1 / §1.3 before emitting SQL.

Within **Type A**, pick one pattern (first match):

| Pattern | When | SQL focus |
|---------|------|-----------|
| Ranking / TopN | 排名 / TopN / 前 N | GROUP BY rank dim + `ORDER BY` + `LIMIT N` |
| Aggregation | 各 / 各自 / 分别 / 按 `<dim>` 分组或汇聚（含「按 `<dim>` 汇聚全部 X」）; 同一维多取值; per-bucket / 趋势 / 每天 / 每小时 / 按天分组 | **SELECT 与 GROUP BY** MUST contain the involved dimensions；取值子集另加 WHERE（`IN` / `OR`），不可只过滤不投影该维 |
| Direct value | Single-object metric with no per-member / group wording above | One row: SELECT aggregates only; equality filters in WHERE; no `GROUP BY` |

## 1. Type A: Metric Query

### 1.1 Common Rules

- Descriptions starting with `维度` / `指标` are dimensions / metrics.
- **SELECT / GROUP BY** follow the Step 0 pattern: Aggregation / TopN / per-bucket dims only. Direct value keeps filter dims in WHERE and does not project them. Metrics **never** appear bare in SELECT: always `SUM(metric)` (or schema aggregate) and stay out of GROUP BY.
- **Mandatory aggregation:** Any Type A query that collapses rows (Direct value, Aggregation / per-bucket, TopN, or 「不按 X 过滤 / 汇聚全部 X」) **must** wrap every `指标` in `SUM()`.
- **Question formula → SQL rewrite:** Treat `result=expr` as column algebra. Map each metric in `expr` to `SUM(metric)`; keep `NULLIF` / `::numeric` / operators. Example: `a/NULLIF(b,0)` → `SUM(a)::numeric / NULLIF(SUM(b), 0)`.
- **Literal priority** (first match wins): (1) question `key=<integer>` → unquoted int; (2) column `example:` style; (3) else INTEGER/BIGINT unquoted, TEXT quoted. Never quote INTEGER/BIGINT.
- WHERE = dimension filters + time window; HAVING = aggregated metric thresholds. Put a dimension in WHERE only when other values must be excluded from the **entire** result.
- 「不按 X 过滤」「汇聚全部 X」: do **not** put X in WHERE or GROUP BY; collapse X by `SUM()` on metrics.
- Table already matches requested grain — do not re-bucket `time`（无 `date_trunc` 等）。**「粒度…」** only selects that table grain, not per-bucket output. Include bare `time` in SELECT / GROUP BY / ORDER BY only for 时间序列 / 趋势 / 每小时 / 按时间点 / per-bucket wording（如「每天」「按天分组」）；a time **range**（过去 N … / 最近 N … / 今天）or bare 「粒度…」 alone does not. When `time` is used, write the column name as-is.
- **ORDER BY:** only for TopN / 排名 / 排序 / 趋势时间序; otherwise omit.
- Division: `SUM(...)::numeric` + `NULLIF(SUM(...), 0)` on denominators. Multiply by 100 only when percent is required. Prefer column `relation_formula` when present, still with `SUM` when aggregating.

### 1.2 Pattern Notes

**Direct value:** WHERE filters + window; SELECT `SUM(...)` only; omit `time`. Use only when Step 0 selected Direct value.

**Aggregation / TopN:** follow Step 0 SQL focus; add `time` only for per-bucket/trend; TopN uses `ORDER BY` + `LIMIT N`; metrics stay `SUM(...)`. Shape: `SELECT <dim>[, time], SUM(<metric>) ... [WHERE <dim> IN (...)] GROUP BY <dim>[, time]`.

### 1.3 Derived Metric Formulas

`time` = Unix epoch seconds (bucket start). **`business_time_window_seconds`** = rate divisor (业务时间窗), separate from `time`.

Unless a formula below says otherwise, **every metric column is wrapped in `SUM()`**. Question-provided formulas follow §1.1 rewrite.

**Traffic volume** (流量): `SUM(uplink_volume)` / `SUM(downlink_volume)` / `SUM(uplink_volume) + SUM(downlink_volume)`. If question says NULL 按 0: `SUM(COALESCE(uplink_volume, 0) + COALESCE(downlink_volume, 0))`.

**Bandwidth / rate** (带宽 / 上下行速率):

- Total: `(SUM(uplink_volume::numeric) + SUM(downlink_volume::numeric)) / NULLIF(business_time_window_seconds, 0)`
- Uplink / Downlink: `SUM(<col>::numeric) / NULLIF(business_time_window_seconds, 0)`

`business_time_window_seconds`: whole-period → query window seconds（最近5分钟=`300`，1小时=`3600`，1天=`86400`）；per-bucket → table grain（`5min`=`300`，`1h`=`3600`，`1d`=`86400`，`1w`=`604800`，`1m`=`2592000`）.

**Traffic ratio:** `SUM(uplink_volume::numeric) / NULLIF(SUM(downlink_volume::numeric), 0)`

**Counts:** `SUM(connections)`, `SUM(subs_count)`

**Weighted average** (sum÷count，如平均时延): `SUM(<total>)::numeric / NULLIF(SUM(<cnt>), 0)`. Prefer columns from the question formula when given; alias = question result name when given.

### 1.4 Share / Success-Rate (占比 / 成功率 / 命中率)

When schema has no dedicated share column and the question asks for 占比 / 成功率 / 命中率 / 分布占比 / `(当前行/全体总和)`, pick **one** shape below. **No** `OVER`; **no** `WITH`. Multiply by 100 only if percent is required.

#### A. Named subset vs all（某一取值 vs 全体）

- Numerator = metric on the named subset; denominator = **same** metric over **all** values of that splitting dim (unless question restricts denominator). Literals via §1.1.
- Do **not** put the subset dim in WHERE.

```text
SUM(CASE WHEN <dim> = <subset_literal> THEN <metric> ELSE 0 END)::numeric
  / NULLIF(SUM(<metric>), 0)
```

```sql
SELECT <dims_incl_time_if_needed>,
  (SUM(CASE WHEN <dim> = <subset_literal> THEN <metric> ELSE 0 END)::numeric
   / NULLIF(SUM(<metric>), 0)) * <100_if_percent_else_omit> AS <alias>
FROM <table>
WHERE <time_and_non_subset_filters>
GROUP BY <dims_incl_time_if_needed>
```

#### B. Distribution across a group dim（按维分组的分布占比）

Use when share = **this group’s metric ÷ sum of the same metric over all groups** under the same filters.

- Outer: `GROUP BY` the distribution dim (plus `time` only if per-bucket). Time range stays in WHERE.
- Denominator: scalar subquery on the **same** fact table as outer `FROM`, same non-distribution filters and time bounds, no `GROUP BY`, same `<metric_expr>`. Do not alter the fact table name.
- Aliases follow the question when given.

```sql
SELECT <dim_or_label>,
  SUM(<metric_expr>) AS <metric_alias>,
  (SUM(<metric_expr>)::numeric
    / NULLIF((
      SELECT SUM(<metric_expr>)
      FROM <same_fact_table> t2
      WHERE <same_non_distribution_filters_and_time>
    ), 0)) * <100_if_percent_else_omit> AS <share_alias>
FROM <same_fact_table> t
  <optional_dim_joins>
WHERE <non_distribution_filters_and_time>
GROUP BY <dim_or_label>
```

Per-bucket distribution: outer `GROUP BY` includes `time`; denominator subquery adds `AND t2.time = t.time` (same filters otherwise).

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
- **Interval syntax:** write every timestamp offset as `INTERVAL 'N unit'` (e.g. `date_trunc('day', NOW()) - INTERVAL '1 day'`).

| Window | Bounds |
|--------|--------|
| 今天 | `date_trunc('day', NOW())` → `NOW()` |
| 本周 / 这周 | `date_trunc('week', NOW())` → `NOW()` |
| 本月 | `date_trunc('month', NOW())` → `NOW()` |
| 最近 N … | `NOW() - INTERVAL '…'` → `NOW()` |
| 昨天 | `[date_trunc('day', NOW()) - INTERVAL '1 day', date_trunc('day', NOW()))` |
| 上周 | `[date_trunc('week', NOW()) - INTERVAL '1 week', date_trunc('week', NOW()))` |
| 上个月 | `[date_trunc('month', NOW()) - INTERVAL '1 month', date_trunc('month', NOW()))` |

Absolute date/time: half-open; omit year → current year; `timestamptz` then epoch.

**Comparison offsets** (named target first; shift current bounds):

- 今天环比昨天 / 昨天环比前天: `INTERVAL '1 day'`
- 上周同一天 / 同比上周 / 本周环比上周 / 与上周同一天同比: `INTERVAL '7 days'`（**not** `1 year`）
- 本月环比上月 / 同比上月: `INTERVAL '1 month'`
- 最近 N 小时/天环比: `INTERVAL 'N hours|days'`
- 同比去年 / 去年同期 / bare 同比 with no period named: `INTERVAL '1 year'`

## 4. General Rules

- Only tables/columns in Database Schema. Literals: §1.1.
- PostgreSQL; no backticks; ASCII aliases; `IS NULL` / `IS NOT NULL` only for null predicates.
- **No** window functions (`OVER (...)`); **no** `WITH` (CTE) — use nested `SELECT` / join of subqueries.
- Output aliases are projections only. Advanced functions only if schema/evidence confirms them.

# Output

Respond with ONLY the SQL query, enclosed in a ```sql``` block.
