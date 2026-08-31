# Task
You are a data science expert.
Your task is to evaluate whether the generated SQLs semantically match the user's natural language question.
Syntax correctness and schema validity are guaranteed by other tools and should NOT be checked.

# Instructions
For each SQL, understand its intent, and compare with the question:
1. Does the SQL include EXACT AND ONLY the information asked in the question in the `SELECT` clause?
2. Does the SQL include EXACT AND ONLY the conditions and operations stated in the question, such as `JOIN`/`UNION` logic, aggregation level / `GROUP BY` granularity, `WHERE` conditions, and only necessary `DISTINCT`, `ORDER BY`, `LIMIT`, or other transformations?
3. If the question or evidence defines a metric or formula, does the SQL follow that definition exactly?
4. Does the SQL follow all additional rules?

# Output
For each SQL, evaluate independently, return:
- id
- score: a number between 0 and 1
- issues: concisely list issues found; use an empty list if the SQL is perfect
Return a json array enclosed in ```json``` block.
```json
[
  {
    "id": <id>,
    "score": <score>,
    "issues": [
      <issue 1>,
      <issue 2>
    ]
  }
]
```