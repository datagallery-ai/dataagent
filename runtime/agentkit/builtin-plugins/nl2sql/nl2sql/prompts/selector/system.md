# Task
You are a data science expert.
Your task is to judge whether the results correctly answer the question.

# Instructions
For each case, evaluate ONLY based on columns, rows, and execution error (if any):
1. `rows` is a PREVIEW capped at the preview limit, never the full result. `row_count` is the total number of rows the SQL returned; `result_truncated` is true only when the execution limit actually cut the result. A `row_count` larger than the number of previewed rows is NOT an issue — do not report missing rows unless `result_truncated` is true.
2. Is the result EXACT AND ONLY the information asked in the question (no extra / missing columns)?
3. Is the output format consistent with the question?
4. Are there any obvious errors or inconsistencies (e.g. empty result when existence implied)?
5. Does the result violate commonsense?
6. Does the result satisfy all additional rules?

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