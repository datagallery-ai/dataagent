# SQL Generation Rules

## 1. Classify Query Type

First classify the question into exactly one type.

Type A: Ordinary metric query
- Use this type when the question has no 同比, 环比, 对比, 与前一天对比, 与上周对比, 保障提升率, or similar comparison/improvement wording.

Type B: Comparison query
- Use this type when the question has 同比, 环比, 对比, 与前一天对比, 与上周对比, or similar comparison wording.
- If the question asks 同比/环比/对比 of 保障提升率, use Type B and use the Type C assurance_improvement_rate expression as metric_expression.

Type C: Assurance improvement rate query
- Use this type when the question asks 保障提升率, 保障效果提升率, 质差保障提升率, or asks for the change from 保障签约用户质差未保障 to 保障签约用户质差保障/质差保障中.
- This is a same-period state-change query, not a time comparison query.

Apply only the rules for the selected type.

## 2. Ordinary Metric Query

### Hard Rules

- Schema descriptions starting with '维度' are dimension fields.
- Schema descriptions starting with '指标' are metric fields.
- First identify all dimensions involved in the question.
- A dimension is involved if the question mentions its field name, alias, semantic meaning, concrete value, filter condition, grouping wording, or ranking wording.
- Output grain is exactly all involved dimensions.
- Every involved dimension MUST appear in both SELECT and GROUP BY.
- A dimension used in WHERE is still an involved dimension. Filtering a dimension does NOT remove it from SELECT or GROUP BY.
- Any time range, calendar time, rolling time, or time-granularity phrase makes time an involved dimension, such as 今天, 本周, 本月, 最近15分钟, 最近1小时, 最近一天, 每天, 每小时, 按时间, 时间粒度.
- If time is an involved dimension, time MUST appear in both SELECT and GROUP BY.
- Only skip GROUP BY time when the user explicitly asks for whole-period aggregation, such as 整体, 总数, 合计, 累计, 总量, 整体汇总.
- Do not treat metric names containing 总, such as 总流量 or 总时长, as whole-period aggregation requests.
- Dimensions in the table but not involved in the question are hidden detail dimensions. Do not SELECT or GROUP BY hidden detail dimensions.
- Every metric field MUST be aggregated at the output grain.
- Additive/count/duration/traffic/PRB usage metrics use SUM(metric::numeric).
- Do NOT directly SELECT a raw metric field in an ordinary metric query.
- SQL is invalid if it selects a metric field without aggregation.
- SQL is invalid if any involved dimension is missing from SELECT or GROUP BY.

### Required SQL Shape

SELECT
    time
    involved_dimension_1,
    involved_dimension_2,
    SUM(metric::numeric) AS metric
FROM table_name
WHERE
    time_filter
    AND dimension_filters
GROUP BY
    time
    involved_dimension_1,
    involved_dimension_2

### Self-Check Before Generating

Before you output SQL, verify:

- [ ] Is `time` in SELECT and GROUP BY (unless whole-period total)?
- [ ] Is every dimension from WHERE also in SELECT and GROUP BY?
- [ ] Are all metrics wrapped in SUM()?
- [ ] If any answer is "NO", fix it before generating.

## 3. Comparison Query

Use this framework:

WITH current_period AS (
    SELECT
        metric_expression AS current_value
    FROM table_name
    WHERE
        current_time_filter
        AND other_filters
),
comparison_period AS (
    SELECT
        metric_expression AS comparison_value
    FROM table_name
    WHERE
        comparison_time_filter
        AND other_filters
)
SELECT
    cp.current_value,
    cmp.comparison_value,
    (cp.current_value::numeric - cmp.comparison_value::numeric)
        / NULLIF(cmp.comparison_value::numeric, 0) AS change_rate
FROM current_period cp, comparison_period cmp

Rules:
- current_time_filter is built from the ordinary time window in Section 5.
- comparison_time_filter is built by applying offset to the current time window timestamp boundaries before epoch conversion.
- Use the same metric_expression and other_filters in current_period and comparison_period.
- If comparison output has involved non-time dimensions, both CTEs SELECT and GROUP BY those same dimensions, and final SELECT joins on exactly those dimensions.

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

## 4. Assurance Improvement Rate Query

保障提升率 is the change rate from the baseline state "保障签约用户质差未保障" to the improved state "保障签约用户质差保障/质差保障中".

Enum mapping:
- Baseline state: guarantee_group = 2
- Improved state: guarantee_group = 3

Formula:
- assurance_improvement_rate = (improved_value - baseline_value) / NULLIF(baseline_value, 0)

Rules:
- Use the same time_filter and other_filters for both states.
- Use the same metric_expression for both states.
- Only change the guarantee_group filter: baseline CTE uses guarantee_group = 2, improved CTE uses guarantee_group = 3.
- Do not SELECT or GROUP BY guarantee_group for this query type unless the user explicitly asks to list guarantee_group itself.
- If the question names a metric, use that metric's normal aggregation/formula as metric_expression.
- If the question only asks 保障提升率 without naming a metric, measure both states by user/count metric when available, preferring SUM(total_subs_count::numeric), then SUM(exp_subs_count::numeric), otherwise COUNT(*).
- If output has involved non-time dimensions, both CTEs SELECT and GROUP BY those same dimensions, and final SELECT joins on exactly those dimensions.
- Time granularity rule: "xx时间粒度/颗粒度" = "aggregate by xx time interval" → "time" in SELECT/GROUP BY (both CTEs) + JOIN on "time" (final SELECT)

Required SQL Shape:

WITH baseline_state AS (
    SELECT
        metric_expression AS baseline_value
    FROM table_name
    WHERE
        time_filter
        AND other_filters
        AND guarantee_group = 2
),
improved_state AS (
    SELECT
        metric_expression AS improved_value
    FROM table_name
    WHERE
        time_filter
        AND other_filters
        AND guarantee_group = 3
)
SELECT
    bs.baseline_value,
    isv.improved_value,
    (isv.improved_value::numeric - bs.baseline_value::numeric)
        / NULLIF(bs.baseline_value::numeric, 0) AS assurance_improvement_rate
FROM baseline_state bs, improved_state isv

## 5. Time Windows

All time filters:
- time stores Unix epoch seconds and represents bucket start.
- All time filters are half-open: time >= start_epoch AND time < end_epoch.
- Every time boundary must be EXTRACT(EPOCH FROM timestamp_expression)::bigint.
- Never compare time directly with timestamp expressions such as NOW() or date_trunc(...).
- Never use <= as upper time boundary.
- Use date_trunc(..., NOW()) for calendar boundaries.
- Use INTERVAL inside timestamp expressions.
- Do not use numeric epoch arithmetic.
- Do not use MAX(time) as time anchor.
- Time granularity (e.g., 15min, 1h, 1d) only determines aggregation granularity and does NOT affect time window boundaries. The time window is purely defined by the user's natural time specification; granularity only impacts GROUP BY output intervals, not WHERE time filters.

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
- Construct boundaries with make_timestamptz(year, month, day, hour, minute, 0), never make_timestamp(...); if the year is omitted, use EXTRACT(YEAR FROM NOW())::int as year, then convert the complete timestamp to epoch.

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

## 6. General Rules

- Use only tables and columns in Database Schema.
- Schema example: values are authoritative for enum/literal values.
- Generated SQL must contain zero backtick characters.
- Use unquoted identifiers by default. If quoting is required, use PostgreSQL double quotes.
- Additive metrics use SUM(metric::numeric).
- Count-like metric columns use SUM(metric), not COUNT(metric).
- Ratios, rates, averages, percentages, proportions, and change rates must use NUMERIC division.
- Rates, success rates, percentages, proportions, and change rates are raw ratios. Do not multiply by 100.
- For AMF, PCF, NWDAF generic wording, do not filter ne_name = 'AMF', 'PCF', or 'NWDAF'. These are type labels, not instance values.
- Temporarily never add info_indicate filters.
- Do not infer exp_opt_flg, guarantee_group IS NOT NULL, or other unrequested filters.
- Use IS NULL or IS NOT NULL. Do not compare metrics with 'NULL' or empty string.
- Do not invent SPLIT_PART, regex, JSON extraction, delimiters, unit conversions, ::hll, or HLL functions unless schema or evidence confirms them.
