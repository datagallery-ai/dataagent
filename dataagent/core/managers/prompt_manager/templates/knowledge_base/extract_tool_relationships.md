Here is an execution plan and data_schemas for extracting relationships between tools and table columns, as well as between tools. The execution plan shows the workflow; tools are those actually executed, each with its name and parameters for relationship extraction.

<start_of_execution_plan>
{{ script }}
<end_of_execution_plan>

<start_of_data_schemas>
{{ data_schemas }}
<end_of_data_schemas>

Your task is to find all relevant connections between tools and all mentioned table columns.
1. Only use tables and columns listed in the data_schemas section; do not create or infer any new tables or columns.
2. Only use tools listed in the tools section; do not create or infer any new tools.
3. DO not include duplicate relationships in your output; each relationship should appear only once.
4. The type of relationship between table columns and tools can only be "is_input" and "outputs_to";
5. The type of relasionship between tools and tools can only be "provides_input_to";
6. If multiple table columns are mentioned in the plan or tools, list their relationships to the tools separately; 
7. If multiple tools are mentioned in the plan or tools, list their relationships to the other tools separately, where the first provides input to the second tool, not the other way around;
8. Your output MUST be a list of dictionaries. Example: [{"column":"", "relationship":"", "toolname":""}, {"toolname1":"", "relationship":"", "toolname2":""}, ...]. Column name must in the format "{table} -> {columnname}"
9. Do not include any other words in the final output, strictly stick to the given output format, and do not include words like ```json```.
