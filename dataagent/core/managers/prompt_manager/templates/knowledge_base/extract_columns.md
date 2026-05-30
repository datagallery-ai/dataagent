You are given an execution plan and data schemas. Your task is to extract all table columns that are referenced in the plan. Only include columns that belong to data schemas.

<start_of_plan>
{{ plan }}
<end_of_plan>

<start_of_data_schemas>
{{ data_schemas }}
<start_of_data_schemas>

Return strictly follow this exact format, The JSON object must use table names as keys and arrays of column names as values. If no columns are found return {}.:
{"table1":["column1","column2",...],"table2":["column1",...],...}

Do NOT return a JSON array, a plain list, or any other structure. Do NOT include duplicates. Table and column names must be strings.
