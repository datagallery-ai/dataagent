# SQL Generation Rules

## Step 0: Classify the Query Type (MANDATORY - Apply First)

Before applying any other rules, first classify the user question into exactly one of the following types:

- **Type A (Ordinary metric query)**: Use when the question has no 同比, 环比, 对比, 与...对比, 保障提升率, or similar comparison/improvement wording.

- **Type B (Comparison query)**: Use when the question has 同比, 环比, 对比, 与前一天对比, 与上周对比, or similar comparison wording.
  - If the question asks 同比/环比/对比 of 保障提升率, use Type B and use the Type C assurance_improvement_rate expression as the metric_expression.

- **Type C (Assurance improvement rate query)**: Use when the question asks 保障提升率, 保障效果提升率, 质差保障提升率, or asks for the change from 保障签约用户质差未保障 to 保障签约用户质差保障/质差保障中.
  - This is a same-period state-change query, not a time comparison query.

**Then apply only the rules for the selected type.**

## 1. Type A: Ordinary Metric Query

### Rules:

- Schema descriptions starting with '维度' are dimension fields.
- Schema descriptions starting with '指标' are metric fields.
- Identify the dimensions involved in the user question. A dimension is involved only when the question refers to the dimension itself or one of its values.
- SELECT and GROUP BY MUST contain exactly the involved dimensions.
- All metric fields must be aggregated using SUM() (or appropriate aggregate functions) across all dimensions not explicitly involved in the query, and MUST NOT appear in the GROUP BY clause.
- Any time expression makes time an involved dimension. Group by time unless the user explicitly asks for whole-period aggregation.
- Use HAVING for filtering aggregated metric results (with the same expression as SELECT). Use WHERE only for pre-aggregation row filtering when explicitly requested.

### Self-Check Before Generating

Before you output SQL, verify:

- [ ] Do SELECT and GROUP BY contain exactly the dimensions involved in the user question?
- [ ] Are all metric fields aggregated and absent from GROUP BY?

## 2. Type B: Comparison Query

### Rules:
- Identify the current period and comparison period from the user question.
- Build the current period boundaries from Section 5. Build the comparison period by applying the comparison offset to the timestamp boundaries before epoch conversion.
- For whole-period comparisons, use `time` only in WHERE and aggregate each complete period.
- Never join different time windows on raw `time`.
- Both periods must SELECT and GROUP BY the same involved non-time dimensions. The final JOIN must include those dimensions.
- For 同比/环比 queries, ALWAYS calculate `(current_value::numeric - comparison_value::numeric) / NULLIF(comparison_value::numeric, 0) AS change_rate`. For plain 对比 queries without rate wording, return only current and comparison values.

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

## 3. Type C: Assurance Improvement Rate Query

保障提升率 is the change rate from the baseline state "保障签约用户质差未保障" to the improved state "保障签约用户质差保障/质差保障中".

Enum mapping:
- Baseline state: guarantee_group = 2
- Improved state: guarantee_group = 3

Formula:
- assurance_improvement_rate = (improved_value - baseline_value) / NULLIF(baseline_value, 0)

### Rules:
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

## 4. Time Windows Rules

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
- The selected table already provides the requested time granularity. Use `time` unchanged in SELECT, GROUP BY, and ORDER BY; never wrap it in `to_timestamp`, `date_trunc`, or any other expression. Time granularity does not affect WHERE boundaries.

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

## 5. General Rules

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
- **MANDATORY: All division operations MUST use NUMERIC arithmetic. Integer division is NOT allowed. Always cast at least one operand (prefer the numerator) to NUMERIC, e.g., `SUM(metric)::numeric / NULLIF(...)`.**
- No Chinese aliases are allowed in generated SQL.

# Output
Respond with ONLY the SQL query, enclosed in a ```sql``` block. Do not include any explanation, comments, or step-by-step reasoning outside the code block.
