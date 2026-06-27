"""校验器专用 Prompt 模板与工厂
"""
from typing import List, Dict, Any, Optional
from ..common.prompt_utils import (
    get_enhanced_database_schema_profile,
    format_sql_guidance,
    get_sql_guidance,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 统一 JSON 输出契约片段
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_JSON_OUTPUT_CONTRACT = """# Response format:
Return one single JSON object and nothing else. Follow this shape exactly:
{
  "reasoning": "Walk through what you inspected and why the rewrite is correct.",
  "sql": "The finalized SQLite statement on one line, runnable as-is, with no comments and no trailing prose."
}
Keep the "sql" value plain SQLite text. Do not wrap it in code fences and do not emit any characters outside the JSON object."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 执行结果复核 Prompt
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EXECUTION_REVIEW_PROMPT_V1 = """# Your job:
You are a senior SQLite engineer. A candidate query was run against the database and the
outcome looks wrong — it either raised an error, returned nothing, or produced values that
do not match what the question asks for. Diagnose the root cause from the schema and the
execution feedback, then hand back a fixed query.

# Work through these steps:
1. Read the schema so you know the available tables, columns and their relationships.
2. Restate what the question (plus its hint) is really asking for.
3. Compare the previous query and its execution feedback to pin down the failure — a wrong
   column, a broken join path, a bad filter, an aggregation mistake, or empty output.
4. Rewrite the query so it runs cleanly under SQLite and returns the intended rows.

Tip: the "Value Examples" attached to TEXT columns are the closest real values to the
phrases in the question — lean on them to choose the right tables, columns and filters.

{OUTPUT_CONTRACT}

# Materials:
## Schema:
{DATABASE_SCHEMA}

## Question:
{QUESTION}

## Hint:
{HINT}

{SQL_GUIDANCE}

## Previous query:
{QUERY}

## Execution feedback:
{RESULT}

Use everything above to repair the query, then reply with only the JSON object.

# Answer:
"""

EXECUTION_REVIEW_PROMPT = EXECUTION_REVIEW_PROMPT_V1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 规则合规复核 Prompt
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

COMPLIANCE_REVIEW_PROMPT_V1 = """# Your job:
You are a senior SQLite engineer doing a final compliance pass on a candidate query.
Inspect the query against the checklist below, fix every violation you find, and leave the
rest of the query untouched. If the query already satisfies every item, return it unchanged.
Do not introduce changes that are unrelated to the checklist.

# Compliance checklist:
1. Join conditions: an ON clause must connect exactly one pair of columns. If you see
   several equalities chained with OR (for example `Ta.c1 = Tb.c2 OR Ta.c1 = Tb.c3`) or an
   `ON ... IN (...)` form, keep only the single most relevant equality and drop the rest.
2. Qualified star in SELECT: a projection such as `T.*` is ambiguous. Replace it with the
   concrete identifier column(s) the question actually needs.
3. String stitching in SELECT: a projection that glues columns with `|| ' ' ||` should
   instead list the columns separated by commas.
4. Extremum via correlated sub-query: a predicate like `col = (SELECT MAX(col) FROM t)` or
   `col = (SELECT MIN(col) FROM t)` should become `ORDER BY t.col DESC LIMIT 1`
   (use ASC for MIN). Where a sub-query of the form `= (SELECT ... LIMIT n)` appears, prefer
   a JOIN over the nested query.
5. Redundant extremum with LIMIT: when the outer SELECT already applies `LIMIT 1`, wrapping
   the target column in MAX()/MIN() is unnecessary — express the intent with ORDER BY + LIMIT
   instead.
6. Extremum inside ORDER BY: never sort by `MIN(col)`/`MAX(col)` together with LIMIT. Sort by
   the bare column instead; if the statement also has GROUP BY, consider whether the value
   should be aggregated with SUM(col) before ordering.
7. Nullable sort key: when ORDER BY ... LIMIT is used to pick top/bottom rows and the sort
   column may be NULL, add an `IS NOT NULL` guard for that column in the WHERE clause. Skip
   this when the ordering expression already aggregates with SUM() or COUNT().
8. Quoting in time comparisons: in a strftime(...) comparison against a bare multi-digit
   number, wrap that number in single quotes so the comparison stays textual.

Only the table and column names present in the schema are allowed; stay within
SQLite-compatible syntax and cover every condition the question and hint require.

{OUTPUT_CONTRACT}

# Materials:
## Schema:
{DATABASE_SCHEMA}

## Question:
{QUESTION}

## Hint:
{HINT}

{SQL_GUIDANCE}

## Query under review:
{QUERY}

Apply the checklist, repair any violation, then reply with only the JSON object.

# Answer:
"""

COMPLIANCE_REVIEW_PROMPT = COMPLIANCE_REVIEW_PROMPT_V1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PromptFactory — 校验器专用格式化方法
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PromptFactory:
    """校验器专用 Prompt 格式化工厂

    公共方法委托 common.prompt_utils，保持向后兼容。
    """

    # 委托给 common.prompt_utils（保持向后兼容的 staticmethod 接口）
    get_enhanced_database_schema_profile = staticmethod(get_enhanced_database_schema_profile)
    format_sql_guidance = staticmethod(format_sql_guidance)
    get_sql_guidance = staticmethod(get_sql_guidance)

    @staticmethod
    def build_execution_review_prompt(database_schema: str, question: str, hint: str,
                                      sql: str, execution_result: str, sql_guidance: str = "") -> str:
        """构建执行结果复核 prompt"""
        guidance_section = ""  # 执行复核不注入额外 sql_guidance
        return EXECUTION_REVIEW_PROMPT.format(
            OUTPUT_CONTRACT=_JSON_OUTPUT_CONTRACT,
            DATABASE_SCHEMA=database_schema,
            QUESTION=question,
            HINT=hint,
            SQL_GUIDANCE=guidance_section,
            QUERY=sql,
            RESULT=execution_result,
        )

    @staticmethod
    def build_compliance_review_prompt(database_schema: str, question: str, hint: str,
                                       sql: str, sql_guidance: str = "") -> str:
        """构建规则合规复核 prompt（规则已内联，无需外部 suggestions）"""
        guidance_section = f"\n{sql_guidance}" if sql_guidance else ""
        return COMPLIANCE_REVIEW_PROMPT.format(
            OUTPUT_CONTRACT=_JSON_OUTPUT_CONTRACT,
            DATABASE_SCHEMA=database_schema,
            QUESTION=question,
            HINT=hint,
            SQL_GUIDANCE=guidance_section,
            QUERY=sql,
        )
