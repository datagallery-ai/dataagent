"""SQL 生成器 Prompt 模板和工厂

提供 DC / Skeleton / ICL 三套生成策略的 Prompt 模板与格式化方法。
"""
from typing import List, Dict, Any
from ..common.prompt_utils import (
    get_enhanced_database_schema_profile,
    format_sql_guidance,
    get_sql_guidance,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 分治式（Divide-and-Conquer）生成模板
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# V1 备份：JSON 输出契约的初始版本，供回退使用
DIVIDE_CONQUER_SQL_PROMPT_V1 = """
# Role:
You are a senior database engineer. Read the schema carefully, understand how the
tables and columns relate, then write one SQLite query that answers the question.

# Strategy — solve by progressive decomposition:
Work the problem from the top down, then assemble the answer bottom-up:
1. Break the question into smaller information needs, each mapped to a draft SQL idea
   that may still leave placeholders for the parts you have not resolved yet.
2. Resolve each smaller need with a concrete SQL fragment.
3. Substitute the resolved fragments back into the placeholders, layer by layer.
4. The final assembly yields one complete query for the original question.

# Constraints to respect:
- SELECT only the columns the question asks for, in the order it implies; drop anything extra.
- Guard against NULLs with a JOIN or `WHERE <column> IS NOT NULL` when a column may be empty.
- Reference only the tables that are truly required in FROM / JOIN.
- Cover every condition stated in the question.
- Add `DISTINCT` only when uniqueness is genuinely required; let the "Total count" / "Distinct count"
  statistics guide that decision.
- When several similar columns exist across tables, pick the right one by reading the descriptions and hints.
- Do not concatenate strings in the SELECT clause (no `|| ' ' ||` or equivalents).
- Prefer `INNER JOIN` over nested sub-queries.
- Restrict yourself to functions SQLite supports.
- Use `STRFTIME()` for any date arithmetic (e.g. `STRFTIME('%Y', SOMETIME)` for the year).
- Quote any table or column name that contains whitespace.
- Treat the provided "Value Examples" for TEXT columns as strong hints about real stored values.
- Join only along explicit foreign keys. If A links to B and C links to B, never join A and C
  directly — route the path through B.

# Output contract:
Reply with exactly one JSON object and no surrounding text. Use this shape:
{{
  "reasoning": "Your step-by-step decomposition and how the fragments combine into the final query.",
  "sql": "The final SQLite query as a single string, with no comments and no extra prose."
}}
Make sure the value of "sql" is valid SQLite and is properly escaped inside the JSON string.

# Input:
## Database Schema:
{DATABASE_SCHEMA}

## Question:
{QUESTION}

## Hints:
{HINT}

{SQL_GUIDANCE}

Restate the question and hint to yourself, build the query by progressive decomposition,
and simplify with `INNER JOIN` instead of nested `SELECT` wherever possible.

# Output:
"""

# 活跃版本（指向 V1）
DIVIDE_CONQUER_SQL_PROMPT = DIVIDE_CONQUER_SQL_PROMPT_V1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 分步式（Stepwise / Skeleton）生成模板
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# V1 备份：JSON 输出契约的初始版本，供回退使用
STEPWISE_SQL_PROMPT_V1 = """
# Role:
You are an expert SQL developer who builds complex queries methodically.
Tackle the question and schema in three explicit stages:
1. **Plan** — decide which SQL pieces the answer needs and how they fit together.
2. **Skeleton** — sketch the query shape with placeholders for names and conditions.
3. **Finalize** — replace the placeholders with concrete tables, columns and predicates.

# Stage details:

## Stage 1 — Plan the components
Determine what each clause must contribute:
- SELECT: which values, aggregations or computed expressions to return.
- FROM: the base table(s) involved.
- JOIN: the relationships that must be linked.
- WHERE: the filtering predicates.
- GROUP BY: the grouping keys for any aggregation.
- HAVING: filters that apply after aggregation.
- ORDER BY: the required sorting.
- LIMIT: any row cap.
- Sub-queries: whether nesting is needed.
- Special functions: date, string or numeric helpers.

## Stage 2 — Draft the skeleton
Lay out the query structure with placeholders, a clear logical flow and tidy formatting.

## Stage 3 — Finalize the query
Fill in exact schema names, concrete values and predicates from the question, and confirm the logic.

# Constraints to respect:
1. Use the exact table and column names from the schema.
2. Stay within SQLite-compatible syntax and functions.
3. Keep the query logic faithful to what the question asks.
4. Favor efficient JOIN patterns over nested sub-queries when feasible.
5. Use clear aliases and clean formatting.
6. Address every aspect of the question and hint.
7. Join only along explicit foreign keys. If A links to B and C links to B, never join A and C
   directly — route the path through B.

# Output contract:
Reply with exactly one JSON object and no surrounding text. Use this shape:
{{
  "reasoning": "Your plan, the skeleton with placeholders, and how you finalized it.",
  "sql": "The final SQLite query as a single string, with no comments and no extra prose."
}}
Make sure the value of "sql" is valid SQLite and is properly escaped inside the JSON string.

# Input:
## Database Schema:
{DATABASE_SCHEMA}

## Question:
{QUESTION}

## Hint:
{HINT}

{SQL_GUIDANCE}

# Output:
"""

# 活跃版本（指向 V1）
STEPWISE_SQL_PROMPT = STEPWISE_SQL_PROMPT_V1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 示例驱动（Exemplar / ICL）生成模板
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# V1 备份：JSON 输出契约的初始版本，供回退使用
EXEMPLAR_SQL_PROMPT_V1 = """
# Role:
You are a database expert skilled at transferring SQL patterns across domains.
You will receive a target schema, a question, and several solved examples drawn from
other databases. Learn from those examples to answer the target question.

# How to use the examples:
1. **Read each example** — every one pairs a question from another domain with the SQL that solves it.
2. **Spot the reusable patterns** — note how the examples handle:
   - aggregations such as MAX, MIN, COUNT, SUM, AVG
   - the shape of JOINs and sub-queries
   - WHERE-clause filtering
   - string matching and comparisons
   - ORDER BY and LIMIT usage
3. **Transfer to the target** — adapt those patterns to the target schema:
   - align the target's needs with the closest example pattern
   - rework the structure so it fits the target tables and columns
   - keep the logic faithful to the target question

# Constraints to respect:
1. The examples use other schemas — translate the patterns onto the target schema.
2. Watch how comparable concepts map to different column names across schemas.
3. Reuse the structural patterns (JOIN style, sub-query usage, etc.).
4. Stay within SQLite-compatible syntax and functions.
5. Use the exact table and column names from the target schema.
6. Make sure the query genuinely answers the target question.
7. Join only along explicit foreign keys. If A links to B and C links to B, never join A and C
   directly — route the path through B.

# Output contract:
Reply with exactly one JSON object and no surrounding text. Use this shape:
{{
  "reasoning": "Which example patterns you reused and how you adapted them to the target.",
  "sql": "The final SQLite query as a single string, with no comments and no extra prose."
}}
Make sure the value of "sql" is valid SQLite and is properly escaped inside the JSON string.

# Input:
## Few-Shot Examples:
{FEW_SHOT_EXAMPLES}

## Target Database Schema:
{DATABASE_SCHEMA}

## Target Question:
{QUESTION}

## Hint:
{HINT}

{SQL_GUIDANCE}

# Output:
"""

# 活跃版本（指向 V1）
EXEMPLAR_SQL_PROMPT = EXEMPLAR_SQL_PROMPT_V1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PromptFactory — 生成器 Prompt 格式化工厂
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PromptFactory:
    """SQL 生成器 Prompt 格式化工厂"""

    # 委托给 common.prompt_utils
    get_enhanced_database_schema_profile = staticmethod(get_enhanced_database_schema_profile)
    format_sql_guidance = staticmethod(format_sql_guidance)
    get_sql_guidance = staticmethod(get_sql_guidance)

    @staticmethod
    def build_divide_conquer_prompt(database_schema: str, question: str, hint: str, sql_guidance: str = "") -> str:
        """格式化分治式 SQL 生成 prompt"""
        guidance_section = f"\n{sql_guidance}" if sql_guidance else ""
        return DIVIDE_CONQUER_SQL_PROMPT.format(
            DATABASE_SCHEMA=database_schema,
            QUESTION=question,
            HINT=hint,
            SQL_GUIDANCE=guidance_section
        )

    @staticmethod
    def build_stepwise_prompt(database_schema: str, question: str, hint: str, sql_guidance: str = "") -> str:
        """格式化分步式 SQL 生成 prompt"""
        guidance_section = f"\n{sql_guidance}" if sql_guidance else ""
        return STEPWISE_SQL_PROMPT.format(
            DATABASE_SCHEMA=database_schema,
            QUESTION=question,
            HINT=hint,
            SQL_GUIDANCE=guidance_section
        )

    @staticmethod
    def build_exemplar_prompt(
        few_shot_examples: List[Dict[str, Any]],
        database_schema: str,
        question: str,
        hint: str,
        sql_guidance: str = ""
    ) -> str:
        """格式化示例驱动 SQL 生成 prompt"""
        formatted_examples = "\n\n".join(
            f"[Example {i}]\nNL: {example['question']}\nSQL: {example['sql']}"
            for i, example in enumerate(few_shot_examples, 1)
        )
        guidance_section = f"\n{sql_guidance}" if sql_guidance else ""
        return EXEMPLAR_SQL_PROMPT.format(
            FEW_SHOT_EXAMPLES=formatted_examples,
            DATABASE_SCHEMA=database_schema,
            QUESTION=question,
            HINT=hint,
            SQL_GUIDANCE=guidance_section
        )
