You will receive information about multiple columns from a dataset. For each column, process the following:

<start_of_document>
{{ knowledge }}
<end_of_document>

Columns Information:
{{ columns_info_str }}

For EACH column, apply these rules:
1. If the document contains information about the column, summarize and refine the description in English.
2. If no document info and current description is adequate, return it unchanged; otherwise infer from name/values.
3. Keep descriptions concise, without column names or references to other columns.
4. For value choices: if fewer than 10 unique values, list up to 5; otherwise summarize the range.
5. If the document contains specific interpretation about NULL/NA/None values, do include them in the output.
6. Each description should be a single sentence/phrase without line breaks.

Return ONLY a JSON dictionary where keys are column names and values are the refined descriptions.
