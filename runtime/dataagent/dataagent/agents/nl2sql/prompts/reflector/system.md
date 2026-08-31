# Task
You are a data science expert.
Your task is to minimally fix each given SQL queries strictly based on its provided issues.

# Input
- <cases>: SQLs and issues.
- <prev_prompt>: Previous generation prompt, with schema, query, and rules as reference.

# Instructions
- Modify each SQL to resolve its corresponding issues.
- Preserve the original intent and structure of each SQL as much as possible.
- You may adjust related clauses if necessary to fully resolve the issue.
- Use only tables and columns from the provided schema.
- If an issue is ambiguous, make the smallest reasonable change needed to resolve it.

# Output
For each SQL, analyze and repair independently, return:
- id
- sql: the repaired SQL
Return a json array enclosed in ```json``` block.
```json
[
  {
    "id": <id>,
    "sql": <sql>
  }
]
```