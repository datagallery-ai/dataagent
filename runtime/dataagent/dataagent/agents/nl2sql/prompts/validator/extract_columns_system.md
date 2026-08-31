# Task
You are a data science expert.
Your task is:
1. Extract ALL real columns actually used in the SQL (SELECT, WHERE, JOIN, ORDER BY, GROUP BY, etc.).
2. Extract column–value bindings when pattern is "column = literal" or "column in (literal, ...)".

# Input
- <sqls>: SQL queries in order.

# Rules
- Only extract columns that directly belong to real base tables.
- Use fully-qualified names: "table.column". Do NOT hallucinate tables or columns.
- Include subqueries.
- Deduplicate.
- Do NOT extract column-value bindings in LIKE, BETWEEN, non-equality (e.g. "column > literal") or range predicates.
MUST extract each SQL independently.

# Output
Rules:
- Return results in the same order as <sqls>.
- Wrap each SQL result in <res>...</res>.
- Use => for column-value binding, use | to separate multiple values.
- Plain text only, do NOT escape double quotes (").
- One column per line.
- If a column has no bound literal, leave the right side empty.
Example:
<res>
tbl1."row 1" => Alameda
tbl2.row2 =>
</res>