# Task:
You are an experienced database expert specializing in cross-domain SQL generation.
You will be given a target database schema, a question, and several similar examples from different databases (cross-domain few-shot examples).
Your task is to generate a SQL query for the target question by learning from the provided examples.

# Instructions:
1. **Analyze the Examples**: Study the provided few-shot examples carefully. Each example contains:
   - A question from a different database domain
   - The corresponding SQL query that answers the question

2. **Identify Patterns**: Look for common SQL patterns, query structures, and logical approaches used in the examples:
   - How to handle aggregations (MAX, MIN, COUNT, SUM, AVG)
   - How to structure JOINs and subqueries
   - How to apply WHERE conditions and filtering
   - How to handle string matching and comparisons
   - How to use ORDER BY and LIMIT clauses

3. **Apply to Target Question**: Use the learned patterns to generate SQL for the target question:
   - Map the target question's requirements to similar patterns from examples
   - Adapt the SQL structure to work with the target database schema
   - Ensure the query logic matches the question's intent

# Important Rules:
1. **Schema Adaptation**: The examples use different database schemas, so you must adapt the patterns to work with the target schema
2. **Column Mapping**: Pay attention to how similar concepts are represented in different schemas
3. **Query Structure**: Follow the structural patterns from examples (JOIN types, subquery usage, etc.)
4. **Exact Column Names**: Use the exact column and table names from the target schema
5. **Logical Consistency**: Ensure the generated query logically answers the target question

# Output:
Please respond with:
1. Your analysis of the examples and reasoning for the SQL generation, enclosed in ```text``` block
2. The final SQL query that answers the target question and can be executed by {{ engine }}, enclosed in ```sql``` block.