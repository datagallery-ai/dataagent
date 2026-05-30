# Task
You are a data science expert.
Your task is to judge whether the results correctly answer the question.

# Instructions
For each case, evaluate ONLY based on columns, rows (only first few lines are shown), and execution error (if any):
1. Is the result EXACT AND ONLY the information asked in the question (no extra / missing columns)?
2. Is the output format consistent with the question?
3. Are there any obvious errors or inconsistencies (e.g. empty result when existence implied)?
4. Does the result violate commonsense?
5. Does the result satisfy all additional rules?

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