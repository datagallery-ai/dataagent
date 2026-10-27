# Task
You are a data science expert.
Your task is to minimally fix each given SQL queries strictly based on its provided issues.

# Input
- <cases>: SQLs and issues.
- <review_history>: Prior rounds of issues, the repairs made, and the issues reported unresolved.
- <prev_prompt>: Previous generation prompt, with schema, query, and rules as reference.

# Instructions
- Modify each SQL to resolve only its current issues.
- Do not undo a repair recorded in <review_history>. If a current issue conflicts with one, keep the earlier repair and report the current issue in `unresolved`.
- If no edit can resolve an issue, leave that SQL as it is and report the issue in `unresolved`. That happens when the schema or SQL cannot express the requirement, or when the SQL already satisfies it.
- Preserve the original intent and structure of each SQL as much as possible.
- You may adjust related clauses if necessary to fully resolve the issue.
- Use only tables and columns from the provided schema.
- If an issue is ambiguous, make the smallest reasonable change needed to resolve it.

# Output
For each SQL, analyze and repair independently, return:
- id
- sql: the repaired SQL
- unresolved: issues you could not resolve by changing the SQL, each with a short reason; use an empty list when every issue was fixed
Return a json array enclosed in ```json``` block.
```json
[
  {
    "id": <id>,
    "sql": <sql>,
    "unresolved": [
      <issue and why it cannot be fixed by changing SQL>
    ]
  }
]
```
