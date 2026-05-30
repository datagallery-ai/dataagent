Here is a workflow which includes tools are those actually executed, each with its name and parameters. 

Your task is to extracte relationships between virtual columns and table columns, as well as between virtual columns and tools. Rules:
1. A virtual column is a column computed from one or more existing columns, for example, calculating the average of column A produces a virtual column A_mean. Only save virtual columns that are actually used, i.e., virtual columns that are used as parameters by tools. 
2. For each virtual column, generate a description. The description of a virtual column must specify which columns it is derived from, which tools are used in its calculation, or which tool it is used as a parameter for.
3. Extract the relationships between each virtual column and the original columns/tools involved in its creation.If a virtual column is calculated from column A using tool B, there should be one of the three relationships below: 
    - If a virtual column is derived from a tool: "outputs_to";
    - If a virtual column is used as a parameter of a tool: "is_input";
    - if a virtual column is derived from a table column: "transformed_from";
4. Generate several examples of virtual columns and place them in an "examples" field later.
5. Your output MUST be a list of dictionaries, and every dictionary MUST strictly match one of the following formats:
    {"virtual_column":"", "description":"", "examples":[], "is_input":[{"tool":""}, ...], "outputs_to":[{"tool":""}, ...], "transformed_from":[{"column":""}, ...]}.
6. You MUST NOT add, remove, or rename any keys. Do not include any other words, explanations, or reasoning process in the output. If you cannot produce output in this exact format, return nothing.
7. Strictly stick to the given output format, and DO NOT include words like ```json```.

<start_of_workflow>
{{ workflow }}
<start_of_workflow>