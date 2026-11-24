"""SQL revision prompt templates and response protocol from metavisor 6c0dcf4."""

import re

from .base import BaseChecker

EMPTY_RESULT_GUIDANCE = """
    When the execution result is empty (no rows returned), analyze step by step:

    Case 1: Verify WHERE condition values
    - Identify literal values in WHERE clauses (dates, strings, numbers)
    - Cross-check them against "Value Examples" in the schema for format and content mismatches
    - Common mismatches: date format variations (e.g., '2014/4/23' vs '2014-04-23'), text value discrepancies
    - Preserve the original condition if values match the database format

    Case 2: Verify subquery compatibility
    - If the SQL uses a subquery in WHERE (e.g., WHERE col = (SELECT MAX(col) ...)), check whether the subquery result is compatible with the outer query's table relationships
    - The subquery may return a value that exists in one table but not in the joined result set
    - Consider rewriting using ORDER BY ... LIMIT 1 to avoid scope mismatch
    - Preserve the subquery if it correctly filters the intended rows

    Case 3: Verify question-hint consistency
    - Compare the question intent with the hint; if they conflict (e.g., question asks for max value but hint suggests min, or question references a different entity than the hint), treat the question as the authoritative source
    - Rewrite WHERE conditions, aggregation directions (MAX vs MIN), or table selections to align with the question intent
    - Preserve the hint-derived conditions only when they are consistent with the question
"""


ALL_NULL_RESULT_GUIDANCE = """
    When the execution result has rows but all values are NULL, analyze step by step. Identify the Case first: a single all-NULL row with MAX/MIN/SUM/AVG indicates Case 1; multiple all-NULL rows indicate Case 2; otherwise check Case 3.

    Case 1: Verify aggregate functions over an empty input set
    - MAX/MIN/SUM/AVG without GROUP BY return one all-NULL row over zero input rows (COUNT returns 0 instead)
    - Trace the empty input: cross-check WHERE literals against "Value Examples" (e.g., year '2021' vs stored '1998'); verify JOIN conditions connect the intended rows
    - Fix the filter literals or the join path so the aggregate sees real rows
    - Preserve the query if the filter is correct: an all-NULL aggregate row is the correct answer when no data matches

    Case 2: Verify NULL values in selected columns or expressions
    - The selected column itself is NULL on the result rows (sparse columns); ORDER BY ... LIMIT 1 may hit a row missing the target column
    - Arithmetic and `||` concatenation propagate NULL: any NULL operand makes the whole expression NULL
    - Add filters such as `col IS NOT NULL` or `col != ''` (`col > 0` for denominators), or wrap expressions with COALESCE
    - Preserve them if the question genuinely asks for values that may be NULL

    Case 3: Verify scalar subqueries and CASE expressions in SELECT
    - A scalar subquery in SELECT returns NULL when it matches no row; rewrite it as a JOIN or fix its filters
    - A CASE without ELSE returns NULL when no branch matches; add an ELSE fallback or fix the conditions
    - Preserve them if NULL is the intended outcome

    Case 4: Verify question-hint consistency
    - Compare the question intent with the hint; if they conflict, treat the question as the authoritative source
    - Rewrite WHERE conditions, aggregation directions (MAX vs MIN), or table selections to align with the question intent
    - Preserve the hint-derived conditions only when they are consistent with the question
"""


EXECUTION_CHECKER_PROMPT_V1 = """
# Task:
You are an SQL database expert tasked with correcting a SQL query. A previous attempt to run a query did not yield the correct results, either due to errors in execution or because the result returned was empty or unexpected. Your role is to analyze the error based on the provided database schema and the details of the failed execution, and then provide a corrected version of the SQL query.

# Instructions:
1. Review Database Schema:
    - Examine the database schema to understand the database structure.
2. Analyze Query Requirements:
    - Original Question: Consider what information the query is supposed to retrieve.
    - Hint: Use the provided hints to understand the relationships and conditions relevant to the query.
    - Executed SQL Query: Review the SQL query that was previously executed and led to an error or incorrect result.
    - Execution Result: Analyze the outcome of the executed query to identify why it failed (e.g., syntax errors, incorrect column references, logical mistakes).
3. Correct the Query:
    - Modify the SQL query to address the identified issues, ensuring it correctly fetches the requested data according to the database schema and query requirements.
    - Use the retrieved values to help write more accurate conditions when appropriate.
{RESULT_GUIDANCE}
[IMPORTANT]
For key phrases mentioned in the question, we have provided the most similar values within the columns (TEXT-TYPE columns) denoted by "Value Examples". **This is a critical hint to identify the tables/columns that will be used in the SQL query.**

# Output Format:
Please respond with XML code structured as follows.
<reasoning>
    Your detailed reasoning for the SQL query revision, including the detailed analysis of the previous query and the database schema, and try to fix the failed query.
</reasoning>
<result>
    The final revised SQL query that answers the question and can be executed by {DIALECT} directly, ensure there is not any {DIALECT} comment and not any other explanation text in the SQL query.
    The SQL query must not include XML-specific characters (e.g., `&lt;`, `&gt;`, `&amp;`); only SQL-valid characters are allowed.
</result>

# Input:
## Database Schema:
{DATABASE_SCHEMA}

## Question:
{QUESTION}

## Hint:
{HINT}

{SQL_GUIDANCE}

## Previous SQL:
{QUERY}

## Execution Result:
{RESULT}

Based on the question, table schemas, the previous query, and the execution result, analyze the result try to fix the query, and only output the XML code (<reasoning>...</reasoning> and <result>...</result>) as your response.

# Output:
"""


COMMON_CHECKER_PROMPT_V1 = """
# Task:
You are an SQL database expert tasked with correcting a SQL query. An external SQL checker tool has checked the SQL query and provided some suggestions to correct. Your role is to analyze the suggestions from the checker tool, and then based on the provided database schema provide a corrected version of the SQL query.

# Instructions:
1. Review Database Schema:
    - Examine the database schema to understand the database structure.
2. Analyze Query Requirements:
    - Original Question: Consider what information the query is supposed to retrieve.
    - Hint: Use the provided hints to understand the relationships and conditions relevant to the query.
    - SQL Query: Review the SQL query that was previously checked.
    - Modification Suggestions: Review the suggestions provided by the external checker, and think how to modify the SQL to meet the suggestions.
3. Correct the Query:
    - Modify the SQL query based the given Modification Suggestions, ensuring it correctly meet the expected suggestions.

[IMPORTANT]
Your are NOT ALLOWED to do any other modifications which are not listed in given suggestions.

# Output Format:
Please respond with XML code structured as follows.
<reasoning>
    Your detailed reasoning for the SQL query revision, including understanding the given suggestions, analyzing the of the previous query and database schema, and then try to fix the query.
</reasoning>
<result>
    The final revised SQL query that answers the question and can be executed by {DIALECT} directly, ensure there is not any {DIALECT} comment and not any other explanation text in the SQL query.
    The SQL query must not include XML-specific characters (e.g., `&lt;`, `&gt;`, `&amp;`); only SQL-valid characters are allowed.
</result>

# Input:
## Database Schema:
{DATABASE_SCHEMA}

## Question:
{QUESTION}

## Hint:
{HINT}

{SQL_GUIDANCE}

## Previous SQL:
{QUERY}

## Modification Suggestions:
{SUGGESTIONS}

Based on the question, database schemas, previous SQL query and modification suggestions, try to fix the query, and only output the XML code (<reasoning>...</reasoning> and <result>...</result>) as your response.

# Output:
"""


def _strip_reasoning_section(prompt: str) -> str:
    """从 V1 模板动态派生 V2（移除 <reasoning> 段），供思考模式 provider 使用。

    单一 V1 源模板 + 运行时文本变换，避免维护两份几乎相同的模板。
    变换结果与原手写 V2 模板逐字节一致（已验证）。
    """
    # a) 移除 Output Format 中的 <reasoning>...</reasoning> 整段
    prompt = re.sub(r"<reasoning>\n.*?\n</reasoning>\n", "", prompt, flags=re.DOTALL)
    # b) 结尾句同步移除 reasoning 提及
    prompt = prompt.replace(
        "only output the XML code (<reasoning>...</reasoning> and <result>...</result>) as your response",
        "only output the XML code (<result>...</result>) as your response",
    )
    return prompt


def build_prompt(context, sql, kind, suggestions, execution_result, result_type=""):
    dialect = context.get("dialect", "sqlite")
    values = {
        "DIALECT": {"sqlite": "SQLite", "mysql": "MySQL", "postgres": "PostgreSQL"}.get(
            dialect, dialect
        ),
        "DATABASE_SCHEMA": context.get("schema", ""),
        "QUESTION": context.get("question", ""),
        "HINT": context.get("evidence", ""),
        "QUERY": sql,
        "SQL_GUIDANCE": "",
    }
    if kind == "common":
        template = COMMON_CHECKER_PROMPT_V1
        if guidance := context.get("sql_guidance"):
            values["SQL_GUIDANCE"] = "\n" + guidance
        values["SUGGESTIONS"] = "\n".join(suggestions)
    elif kind == "execution":
        template = EXECUTION_CHECKER_PROMPT_V1
        values["RESULT"] = execution_result
        values["RESULT_GUIDANCE"] = {
            "empty_result": EMPTY_RESULT_GUIDANCE,
            "all_null_result": ALL_NULL_RESULT_GUIDANCE,
        }.get(result_type, "")
    else:
        raise ValueError(f"Unknown checker prompt kind: {kind!r}")
    if not context.get("emit_reasoning_tag", True):
        template = _strip_reasoning_section(template)
    return template.format(**values)


def parse_result_sql(response: str) -> str | None:
    """Read the first reference XML result, restoring the LLM stop sequence."""
    return BaseChecker._parse_llm_response(response) or None
