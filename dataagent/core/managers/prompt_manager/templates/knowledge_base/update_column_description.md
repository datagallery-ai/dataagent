You are given two inputs:

An execution plan, which contains step-by-step descriptions, the tools executed, their parameters, and expected outputs.

<start_of_plan>
{{ plan }}
<end_of_plan>

A list of tables, columns, and their old descriptions.

<start_of_old_description>
{{ old_description }}
<end_of_old_description>

Your task is to determine, for each column, whether its description should be updated based on the information in the plan. If an update is needed, provide the new description; if not, keep the old description.

When writing or updating a description, make sure it clearly explains the meaning of the column, what data it contains, and how it can be used.

Return your results as a JSON array of objects. Each object must contain the keys "table", "column", and "new_description". Example valid output:

[
    {"table": "table_name", "column": "column_name", "new_description": "new description"},
    {"table": "table2", "column": "column2", "new_description": null}
]

If a column does not require an update, set "new_description" to null. Output must be valid JSON only; do not include any extra text, comments, or formatting.

Do not include any other information or words in your output. Only use the specified format.
