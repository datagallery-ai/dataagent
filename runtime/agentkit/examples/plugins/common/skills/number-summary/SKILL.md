---
name: number-summary
description: Compute and explain descriptive statistics for an explicit list of numbers.
---

# Number summary

1. Extract the numeric list supplied by the user. Ask if the input is ambiguous.
2. Call `common__summarize_numbers`; do not calculate the answer from memory.
3. Report count, sum, mean, minimum and maximum from the tool result.
4. If the tool rejects the input, correct it only when the user's intent is clear.
5. Return the answer as text. Do not create files or use shell commands.
