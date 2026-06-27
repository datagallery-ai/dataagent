"""
Prompt Templates
"""

EVIDENCE_PARSING_PROMPT = """
You are an expert at extracting structured database references from natural language.

## Task
Parse the input hint and extract all column names, values, and table names mentioned.

## Hint Patterns to Recognize

### Formula/Calculation patterns:
- "X = `Column A` / `Column B`" → columns: Column A, Column B
- "X can be computed by A + B" → columns: A, B

### Reference patterns (refers to / means / stands for):
- "X refers to `Column` = value" → column: Column, value: value
- "X refers to Column = value in table T" → column: Column, value: value, table: T
- "'POPLATEK TYDNE' stands for weekly issuance" → value: POPLATEK TYDNE
- "A11 refers to average salary" → column: A11

### Column name formats:
- Backticks: `Column Name`, `Free Meal Count (K-12)`
- Plain: column_name, NumTstTakr, A11, A3
- Table.column: Country.name, League.name
- SQL functions: MAX(crossing), MIN(time)

### Value formats:
- Equals: = 1, = 'value', = 'F'
- Comparison: > 70, < 1982
- Status codes: status = 'A', type = 'OWNER'

### Table patterns:
- "in table T" or "in the table T"
- Prefix in Table.column format

## Rules
- Extract ALL column/value/table references found in text
- ONLY output fields explicitly mentioned (omit missing fields)
- For formulas, extract each column separately
- Preserve original column names exactly as written

## Input
hint: {evidence}

## Output Format
Return JSON only:
{{"extracted_evidence": [{{"column": "...", "value": "...", "table": "..."}}]}}
"""

KEYWORDS_EXTRACTION_PROMPT = """
Goal: From the question and its hint, pull out the meaningful search terms — single keywords, multi-word keyphrases, and named entities (people, places, organizations, dates, codes). These terms are what later steps use to match against database columns and values.

Guidelines:
- Scan the question for its central subjects, metrics, constraints, and any proper nouns or technical terms.
- Scan the hint as well; it often names the exact fields, codes, or value strings that matter, including expressions hidden inside SQL-style snippets.
- Keep each term written exactly as it appears in the source text — do not paraphrase, translate, or change casing.
- Merge everything into one flat list; prefer specific phrases over generic words, and when useful split a compound entity into both its full form and its salient parts.

Worked examples:

Question: Which supplier delivered the largest total order value to the Berlin warehouse in 2021?
Hint: total order value = SUM(quantity * unit_price); Berlin warehouse refers to warehouse_city = 'Berlin'.
<result>
["supplier", "largest total order value", "Berlin warehouse", "2021", "warehouse_city", "Berlin", "quantity", "unit_price"]
</result>

Question: List the directors whose movies have an average rating above 8 on IMDb.
Hint: average rating refers to AVG(rating); above 8 means rating > 8.
<result>
["directors", "movies", "average rating", "8", "IMDb", "rating"]
</result>

Question: How many patients were prescribed 'Atorvastatin' after their first cardiac surgery?
Hint: prescribed 'Atorvastatin' refers to drug_name = 'Atorvastatin'; first cardiac surgery refers to MIN(surgery_date) where surgery_type = 'cardiac'.
<result>
["patients", "Atorvastatin", "first cardiac surgery", "drug_name", "surgery_date", "surgery_type", "cardiac"]
</result>

Now process the input below.

Question: {QUESTION}
Hint: {HINT}

Respond with nothing but the XML block shown, where the brackets enclose one valid JSON array of strings:

<result>
[your_keywords_json_array]
</result>
"""

SCHEMA_SELECTION_PROMPT = """
# Role:
You are a meticulous database analyst. Given a database schema, a natural-language question, and an auxiliary hint, decide the minimal set of tables and columns needed to build a SQL query that answers the question.

# How to decide:
- The schema lists every table with its columns, primary keys and foreign keys; rely on it to understand the data model.
- The hint points to schema elements that matter for this particular question.
- Tie every kept table/column to a concrete need in the question; keep the reasoning brief.

# Selection rules:
1. A column's "Value Examples" are real values most similar to phrases in the question; treat them as strong evidence that the column is relevant.
2. When unsure about a column, keep it — omitting a needed column is worse than carrying a spare one.
3. Any column whose "Value Examples" match the question MUST be kept.
4. Joins are allowed only along declared foreign keys. If A links to B and C links to B, do NOT join A and C directly; route the join through B.

# Answer format:
First, inside a <reasoning> block, briefly justify your choices — tie each kept table/column to a concrete need in the question or to a matching value example.
Then, inside a <selection> block, output a JSON object that maps each chosen table name to the list of its chosen column names.
<reasoning>
Concise justification for the tables/columns you keep.
</reasoning>
<selection>
{{
  "table_name": ["column_a", "column_b"],
  "another_table": ["column_c"]
}}
</selection>

# Input:
## Database Schema:
{DATABASE_SCHEMA}

## Question:
{QUESTION}

## Hint:
{HINT}

Output the <reasoning> block followed by the <selection> block as described above.
"""

SQL_BACKED_SELECTION_PROMPT = """
# Role:
You are a database expert who reverse-checks which tables and columns a question needs
by first drafting the SQL that would answer it. You will receive a target database
schema, the question, an auxiliary hint, and several solved examples from other databases.

# How to use the examples:
- Each example pairs a question from another domain with the SQL that answers it.
- Study how those examples handle aggregations, JOINs, sub-queries, filtering, string
  matching, ordering and limits, then transfer the closest patterns to the target schema.
- Adapt every borrowed pattern to the target tables and columns; keep the logic faithful
  to the target question.

# Rules to respect:
1. Use the exact table and column names from the target schema.
2. Stay within SQLite-compatible syntax and functions.
3. Cover every condition stated in the question and hint.
4. Join only along explicit foreign keys. If A links to B and C links to B, never join A
   and C directly — route the path through B.
5. The "sql_used_tables" and "sql_used_columns" you report MUST list exactly the tables and
   columns your SQL actually references — do not invent extras and do not omit any used name.

# Output contract:
Reply with exactly one JSON object and no surrounding text. Use this shape:
{{
  "reasoning": "Which example patterns you reused and how you mapped them onto the target.",
  "sql": "The SQLite query that answers the question, as a single string with no comments.",
  "sql_used_tables": ["table1", "table2"],
  "sql_used_columns": ["table1.column1", "table1.column2", "table2.column1"]
}}
Make sure the value of "sql" is valid SQLite and is properly escaped inside the JSON string.
Every entry in "sql_used_columns" MUST use the exact "table_name.column_name" form, and every
table referenced there MUST also appear in "sql_used_tables".

# Input:
## Few-Shot Examples:
{FEW_SHOT_EXAMPLES}

## Target Database Schema:
{DATABASE_SCHEMA}

## Target Question:
{QUESTION}

## Hint:
{HINT}

# Output:
"""
