Based on the following column information, determine the most appropriate data type for each column from this predefined list: {outputs}.

Column Information:
{{ cur_column_info }}

For each column, consider:
- Column name and description
- Sample values and their patterns
- Expected data characteristics

Rules:
1. Choose ONLY from the predefined type list.
2. If no clear match, select the closest appropriate type.
3. If a column doesn't fit any type well, use "Text" as default.
4. Return a JSON dictionary with column names as keys and selected types as values.

Return format:
{
"column1": "Type",
"column2": "Type",
...
}

If any column's inferred type is not in the predefined list, Please re-reason.
