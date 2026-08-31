# SQL Generation Rules

## Step 0: Classify the Query Type (MANDATORY - Apply First)

Classify the question into exactly one type, then apply only that type’s rules:

- **Type A (Ordinary metric query)**: No 同比 / 环比 / 对比 / 与...对比 or similar period-comparison wording.
- **Type B (Comparison query)**: Has 同比 / 环比 / 对比 or similar wording that compares two time periods.

## 1. Type A: Ordinary Metric Query

- Schema descriptions starting with `维度` are dimensions; starting with `指标` are metrics.
- A dimension is involved only when the question refers to the dimension itself or one of its values.
- SELECT and GROUP BY MUST contain exactly the involved dimensions.
- Metric fields must be aggregated (typically `SUM`) across dimensions not explicitly involved, and MUST NOT appear in GROUP BY.
- Any time expression makes `time` an involved dimension unless the user explicitly asks for whole-period aggregation. For trends or explicit time granularity, include `time` in SELECT / GROUP BY and ORDER BY `time`.
- The table already matches the requested time granularity. Use bare `time` in SELECT / GROUP BY / ORDER BY; do not wrap it in `to_timestamp`, `date_trunc`, or other expressions for granularity.
- Use HAVING for filters on aggregated metrics; use WHERE for pre-aggregation filters (including time windows).
- Map user terms to schema columns and example values only; do not invent columns or enum codes.
- Rates, averages, and percentages must use NUMERIC division. If schema provides sum and count bases or a `relation_formula`, use that. Do not multiply by 100 unless explicitly required.

### Self-Check

- [ ] SELECT / GROUP BY match involved dimensions only.
- [ ] Metrics are aggregated and absent from GROUP BY.

## 2. Type B: Comparison Query

- Identify current and comparison periods from the question (Section 3).
- Build timestamp boundaries first; apply `INTERVAL` inside timestamp expressions; then convert with `EXTRACT(EPOCH FROM ...)::bigint`. Never subtract an offset from an already-extracted bigint epoch.
- For whole-period comparisons, use `time` only in WHERE; aggregate each period separately.
- Never join different time windows on raw `time`.
- Both periods MUST SELECT / GROUP BY the same involved non-time dimensions; the final JOIN must be on exactly those dimensions.
- For 同比 / 环比, ALWAYS compute `(current_value::numeric - comparison_value::numeric) / NULLIF(comparison_value::numeric, 0) AS change_rate`.
- For plain 对比 without rate wording, return current and comparison values only.

current_time_filter:

- `time >= EXTRACT(EPOCH FROM current_start_timestamp)::bigint`
- `time < EXTRACT(EPOCH FROM current_end_timestamp)::bigint`

comparison_time_filter:

- `time >= EXTRACT(EPOCH FROM (current_start_timestamp - offset))::bigint`
- `time < EXTRACT(EPOCH FROM (current_end_timestamp - offset))::bigint`

## 3. Time Window Rules

- `time` is Unix epoch seconds and represents bucket start.
- All time filters are half-open: `time >= start_epoch AND time < end_epoch`.
- Every boundary must be `EXTRACT(EPOCH FROM timestamp_expression)::bigint`.
- Never compare `time` directly to `NOW()` / `date_trunc(...)`; never use `<=` as the upper bound.
- Use `date_trunc(..., NOW())` for calendar boundaries; put `INTERVAL` inside timestamp expressions.
- Do not use numeric epoch arithmetic or `MAX(time)` as a time anchor.

Ordinary current windows:

- 今天: `date_trunc('day', NOW())` → `NOW()`
- 本周 / 这周: `date_trunc('week', NOW())` → `NOW()`
- 本月: `date_trunc('month', NOW())` → `NOW()`
- 最近 N 分钟 / 小时 / 天 / 周: `NOW() - INTERVAL '…'` → `NOW()`

Complete historical windows:

- 昨天: `[date_trunc('day', NOW()) - INTERVAL '1 day', date_trunc('day', NOW()))`
- 上周: `[date_trunc('week', NOW()) - INTERVAL '1 week', date_trunc('week', NOW()))`
- 上个月: `[date_trunc('month', NOW()) - INTERVAL '1 month', date_trunc('month', NOW()))`

Absolute date/time windows:

- Half-open. Date-only end date includes the whole end day (exclusive next midnight).
- If year is omitted, use the current calendar year.
- Build `timestamptz` boundaries in session TimeZone, then convert to epoch. Prefer ISO `::timestamptz` or `date_trunc('year', NOW())` + INTERVAL offsets; do not use `make_timestamptz`.

Comparison offsets:

- 今天环比昨天 / 今天和昨天: `INTERVAL '1 day'`
- 本周环比上周 / 同比上周: `INTERVAL '7 days'`
- 本月环比上月 / 同比上月: `INTERVAL '1 month'`
- 最近 N 小时 / 天环比: `INTERVAL 'N hours|days'`
- 同比去年: `INTERVAL '1 year'`

## 4. General Rules

- Use only tables and columns in Database Schema.
- Schema example values are authoritative for enums/literals.
- Generated SQL must contain zero backtick characters.
- Prefer unquoted identifiers; if quoting is required, use PostgreSQL double quotes.
- Use only ASCII for identifiers, aliases, and comments. English aliases only. No Chinese aliases.
- **MANDATORY:** All division MUST use NUMERIC arithmetic (`::numeric`). Use `NULLIF` on denominators.
- Use `IS NULL` / `IS NOT NULL`. Do not compare metrics to `'NULL'` or `''`.
- Do not invent `SPLIT_PART`, regex, JSON extraction, unit conversions, or HLL functions unless schema/evidence confirms them.
- If a column description includes `relation_formula`, use that formula.

# Output

Respond with ONLY the SQL query, enclosed in a ```sql``` block. Do not include any explanation, comments, or step-by-step reasoning outside the code block.
