# SQL Generation Rules

## Step 0: Classify the Query Type (MANDATORY - Apply First)

Before applying any other rules, first determine whether the compared items are time periods or dimension values, then classify the user question into exactly one of the following types:

- **Type A (Ordinary or same-period dimension comparison query)**: Use for ordinary metric queries and for comparisons between dimension values, groups, categories, or states that share the same time filter. Use Type A even when the question contains 对比, 提升, 差异, or a comparison formula, as long as it does not compare two different time periods.

- **Type B (Time-period comparison query)**: Use only when the question compares two different time periods, such as 同比, 环比, 与前一天对比, 与上周对比, or 与去年同期对比. The word 对比 alone is not sufficient to select Type B.

**Then apply only the rules for the selected type.**

## 1. Type A: Ordinary or Same-Period Dimension Comparison Query

### Rules:

- Schema descriptions starting with '维度' are dimension fields.
- Schema descriptions starting with '指标' are metric fields.
- Identify the dimensions involved in the user question. A dimension is involved only when the question refers to the dimension itself or one of its values.
- SELECT and GROUP BY MUST contain exactly the involved dimensions.
- **If the user explicitly requests a time-series breakdown (by mentioning any time granularity such as '1h', '1d', '每小时', '每天', '按小时聚合', '时间粒度', or equivalent), then `time` MUST also be included in SELECT and GROUP BY. This includes phrases like "支持时间粒度" or "建议时间粒度".**
- All metric fields must be aggregated using SUM() (or appropriate aggregate functions) across all dimensions not explicitly involved in the query, and MUST NOT appear in the GROUP BY clause.
- A time range alone does not make time an output dimension; only an explicit time granularity or time-series request does.
- Use HAVING for filtering aggregated metric results (with the same expression as SELECT). Use WHERE only for pre-aggregation row filtering when explicitly requested.
- When the user provides a formula comparing multiple values of the same dimension within one time window, follow the formula exactly, use one shared time filter, and calculate each value with conditional aggregation. Do not create current-period/comparison-period CTEs or apply Type B SQL shapes.

## 2. Type B: Time-Period Comparison Query

Type B applies only after Step 0 identifies a comparison between two different time periods. Then classify the time comparison into exactly one subtype. A question containing two time ranges does not by itself request a time-series output.

### Type B1: Whole-period comparison

Use Type B1 when the question compares two periods but does not explicitly request a time granularity, time-series trend, hourly/daily detail, or values for each time bucket.

Rules:
- Use `time` only in the two period filters. Do not SELECT, GROUP BY, ORDER BY, or JOIN on `time`.
- Aggregate the current period and comparison period independently.
- Both period CTEs must SELECT and GROUP BY the same involved non-time dimensions.
- If there are no involved non-time dimensions, each CTE returns one row and the final SELECT combines those two scalar rows with CROSS JOIN.
- If involved non-time dimensions exist, use FULL OUTER JOIN only on those dimensions so values present in only one period are preserved. Never include `time` in that JOIN.
- For 同比/环比 or explicit rate wording, calculate `(current_value::numeric - comparison_value::numeric) / NULLIF(comparison_value::numeric, 0) AS change_rate` after the two whole-period values are produced.
- For a plain time-period 对比 without rate wording, return the current and comparison values without adding `change_rate`.

### Type B2: Independent time-series comparison

Use Type B2 when two different time periods are compared and the question explicitly requests a time granularity, time-series trend, hourly/daily detail, or separate values for each time bucket, but does not use 同比/环比 wording and does not request a difference, growth amount, change rate, or another calculation between corresponding buckets of those periods.

This is the default SQL shape for time-series comparison.

Rules:
- Generate the current-period series and comparison-period series independently.
- Both SELECT branches must return the same columns in the same order: `period_label`, raw `time`, the same involved non-time dimensions, and the same metric expressions.
- Use the ASCII literals `'current_period'` and `'comparison_period'` as `period_label` values.
- Each branch must apply its own time filter and GROUP BY raw `time` plus the same involved non-time dimensions.
- Combine the two branches with UNION ALL.
- Preserve each period's original raw `time`. Never shift a timestamp merely to make two different periods appear equal.
- Never JOIN the two periods on raw `time`, and do not put current and comparison values into the same row.
- Order the combined result by `period_label`, raw `time`, and then any involved non-time dimensions.

Required SQL Shape:

The schematic omits optional non-time dimensions. When such dimensions are involved, insert the same dimension columns after `time` in both branches and add them to both GROUP BY clauses.

SELECT
    'current_period' AS period_label,
    time,
    metric_expression AS metric_value
FROM table_name
WHERE current_time_filter
GROUP BY time

UNION ALL

SELECT
    'comparison_period' AS period_label,
    time,
    metric_expression AS metric_value
FROM table_name
WHERE comparison_time_filter
GROUP BY time

### Type B3: Per-bucket calculated comparison

Use Type B3 only when two different time periods are compared, the question requests a time-series breakdown, and it either uses 同比/环比 wording or explicitly requests a difference, growth amount, change rate, or another calculation between corresponding buckets of those periods.

Rules:
- Never JOIN different periods on raw `time` because their absolute timestamps are different.
- Derive a zero-based `bucket_index` independently for each period from that period's own start boundary and the selected table's bucket size:
  - `5min` → `bucket_seconds = 300`
  - `15min` → `bucket_seconds = 900`
  - `1h` → `bucket_seconds = 3600`
  - `1d` → `bucket_seconds = 86400`
- Use `FLOOR((time - period_start_epoch)::numeric / bucket_seconds)::bigint AS bucket_index`. This relative index aligns the first bucket of one arbitrary absolute period with the first bucket of the other without changing either raw timestamp.
- Each period CTE must retain its own raw timestamp as `current_time` or `comparison_time`, aggregate by raw `time` and the same involved non-time dimensions, and calculate its own `bucket_index`.
- Use FULL OUTER JOIN on `bucket_index` plus all involved non-time dimensions so missing or extra buckets from either period are preserved.
- Return both raw timestamps, both metric values, and the requested calculated difference or rate.
- For 同比/环比 or explicit rate wording, calculate `(current_value::numeric - comparison_value::numeric) / NULLIF(comparison_value::numeric, 0) AS change_rate` only after relative bucket alignment.

### Time boundaries shared by all Type B subtypes

- Identify the current period and comparison period from the user question.
- Build all time boundaries according to Section 3.
- If the user explicitly gives both absolute periods, build each period directly from its own stated boundaries; do not invent an offset between them.
- Otherwise, build the current period first and derive the comparison period by applying the requested comparison offset to the timestamp boundaries before epoch conversion.

current_time_filter:
- time >= EXTRACT(EPOCH FROM current_start_timestamp)::bigint
- time < EXTRACT(EPOCH FROM current_end_timestamp)::bigint

comparison_time_filter:
- time >= EXTRACT(EPOCH FROM (current_start_timestamp - offset))::bigint
- time < EXTRACT(EPOCH FROM (current_end_timestamp - offset))::bigint

Important:
- Build timestamp boundaries first, apply INTERVAL inside the timestamp expression, then convert the final boundary with EXTRACT(EPOCH FROM ... )::bigint.
- Never subtract offset from an already-extracted bigint epoch value.
- Do not replace comparison_time_filter with a complete calendar period unless the user explicitly asks for a complete historical period.
- The Type B3 `bucket_index` calculation is not a time boundary and is the only permitted relative numeric epoch calculation.

## 3. Time Windows Rules

All time filters:
- time stores Unix epoch seconds and represents bucket start.
- All time filters are half-open: time >= start_epoch AND time < end_epoch.
- Every time boundary must be EXTRACT(EPOCH FROM timestamp_expression)::bigint.
- Never compare time directly with timestamp expressions such as NOW() or date_trunc(...).
- Never use <= as upper time boundary.
- Use date_trunc(..., NOW()) for calendar boundaries.
- Use INTERVAL inside timestamp expressions.
- Do not use numeric epoch arithmetic to construct time-window boundaries or to shift one period's raw timestamp onto another period. Type B3 may subtract its own period start epoch only to calculate `bucket_index`.
- Do not use MAX(time) as time anchor.
- The physical granularity of the selected table alone does not require grouping or outputting time.
- Only when the question explicitly requests a time-series output, use the selected table's raw `time` unchanged in SELECT, GROUP BY, and ORDER BY; never wrap it in `to_timestamp`, `date_trunc`, or any other expression. Type B3 may additionally derive `bucket_index` from raw `time` without replacing the raw timestamp. Time granularity does not affect WHERE boundaries.

Ordinary current windows:
- 今天: current_start_timestamp = date_trunc('day', NOW()), current_end_timestamp = NOW()
- 本周 or 这周: current_start_timestamp = date_trunc('week', NOW()), current_end_timestamp = NOW()
- 本月: current_start_timestamp = date_trunc('month', NOW()), current_end_timestamp = NOW()
- 最近15分钟: current_start_timestamp = NOW() - INTERVAL '15 minutes', current_end_timestamp = NOW()
- 最近1小时: current_start_timestamp = NOW() - INTERVAL '1 hour', current_end_timestamp = NOW()
- 最近一天: current_start_timestamp = NOW() - INTERVAL '1 day', current_end_timestamp = NOW()
- 最近一周: current_start_timestamp = NOW() - INTERVAL '7 days', current_end_timestamp = NOW()

Absolute date/time windows:
- Always use a half-open interval: time >= start_epoch AND time < end_epoch.
- If the year is omitted, use the current calendar year.
- A date-only range includes the entire end date. Example: “6月16日到6月21日” means [June 16 00:00, June 22 00:00).
- A range with explicit clock times uses those exact boundaries. Example: “6月16日8点到12点” means [June 16 08:00, June 16 12:00).
- Never replace an explicit date/time range with a recent-N window or round it according to table granularity.
- Interpret all absolute date/time boundaries in the current PostgreSQL session TimeZone.
- Construct absolute boundaries as `timestamptz`: if the year is specified, cast an ISO `YYYY-MM-DD HH24:MI:SS` literal with `::timestamptz`; if the year is omitted, use `date_trunc('year', NOW())` plus calendar `INTERVAL` offsets for month - 1, day - 1, hour, minute, and second; then convert the complete timestamp to epoch. Never use `make_timestamptz` or `make_timestamp`.

Complete historical windows:
- 昨天: start_timestamp = date_trunc('day', NOW()) - INTERVAL '1 day', end_timestamp = date_trunc('day', NOW())
- 上周: start_timestamp = date_trunc('week', NOW()) - INTERVAL '1 week', end_timestamp = date_trunc('week', NOW())
- 上个月: start_timestamp = date_trunc('month', NOW()) - INTERVAL '1 month', end_timestamp = date_trunc('month', NOW())

Offset rules for comparison:
- 最近N分钟环比: offset = INTERVAL 'N minutes'
- 最近N小时环比: offset = INTERVAL 'N hours'
- 最近N天环比: offset = INTERVAL 'N days'
- 今天环比昨天: offset = INTERVAL '1 day'
- 本周环比上周: offset = INTERVAL '7 days'
- 本月环比上月: offset = INTERVAL '1 month'
- 同比上周 or 与上周对比: offset = INTERVAL '7 days'
- 同比上月 or 与上月对比: offset = INTERVAL '1 month'
- 同比去年 or 与去年对比: offset = INTERVAL '1 year'

Comparison examples:
- 最近15分钟环比:
  current_time_filter:
  time >= EXTRACT(EPOCH FROM (NOW() - INTERVAL '15 minutes'))::bigint
  AND time < EXTRACT(EPOCH FROM NOW())::bigint
  comparison_time_filter:
  time >= EXTRACT(EPOCH FROM (NOW() - INTERVAL '30 minutes'))::bigint
  AND time < EXTRACT(EPOCH FROM (NOW() - INTERVAL '15 minutes'))::bigint

- 今天同比上周:
  current_time_filter:
  time >= EXTRACT(EPOCH FROM date_trunc('day', NOW()))::bigint
  AND time < EXTRACT(EPOCH FROM NOW())::bigint
  comparison_time_filter:
  time >= EXTRACT(EPOCH FROM (date_trunc('day', NOW()) - INTERVAL '7 days'))::bigint
  AND time < EXTRACT(EPOCH FROM (NOW() - INTERVAL '7 days'))::bigint

- 本月同比上月:
  current_time_filter:
  time >= EXTRACT(EPOCH FROM date_trunc('month', NOW()))::bigint
  AND time < EXTRACT(EPOCH FROM NOW())::bigint
  comparison_time_filter:
  time >= EXTRACT(EPOCH FROM (date_trunc('month', NOW()) - INTERVAL '1 month'))::bigint
  AND time < EXTRACT(EPOCH FROM (NOW() - INTERVAL '1 month'))::bigint

## 4. General Rules

- Use only tables and columns in Database Schema.
- Schema example: values are authoritative for enum/literal values.
- Generated SQL must contain zero backtick characters.
- Use unquoted identifiers by default. If quoting is required, use PostgreSQL double quotes.
- Use only ASCII characters for all identifiers, aliases, and comments. Use English aliases.
- Ratios, rates, averages, percentages, proportions, and change rates must use NUMERIC division.
- Rates, success rates, percentages, proportions, and change rates are raw ratios. Do not multiply by 100.
- For AMF, PCF, NWDAF generic wording, do not filter ne_name = 'AMF', 'PCF', or 'NWDAF'. These are type labels, not instance values.
- Temporarily never add info_indicate filters.
- Use IS NULL or IS NOT NULL. Do not compare metrics with 'NULL' or empty string.
- Do not invent SPLIT_PART, regex, JSON extraction, delimiters, unit conversions, ::hll, or HLL functions unless schema or evidence confirms them.
- If a column has `relation_formula` in its description, use that formula to compute the metric.
- When returning dimension members that are filtered, ranked, or ordered by a metric, SELECT must include both the involved dimensions and the computed metric value. Reuse exactly the same aggregate or formula expression in SELECT and HAVING/ORDER BY; do not return only the dimensions. If the user explicitly asks only for the count of qualifying members, return the count only.
- **MANDATORY: All division operations MUST use NUMERIC arithmetic. Integer division is NOT allowed. Always cast at least one operand (prefer the numerator) to NUMERIC, e.g., `SUM(metric)::numeric / NULLIF(...)`.**
- **STRICTLY FORBIDDEN: Chinese aliases are NOT allowed in generated SQL. All aliases MUST use ASCII characters and English naming. Violations will be rejected.**

