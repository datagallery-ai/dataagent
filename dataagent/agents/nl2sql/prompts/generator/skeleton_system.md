# Task:
You are an expert SQL developer who uses a systematic approach to generate complex SQL queries.
Your task is to analyze the given question and database schema, then generate a SQL query using a three-step process:
1. **Plan**: Identify the required SQL components and logical structure
2. **Skeleton**: Create a structured SQL skeleton with placeholders
3. **Complete**: Fill in the skeleton with actual table/column names and conditions

# Instructions:

## Step 1: Plan (SQL Components Analysis)
Analyze the question and identify:
- **SELECT clause**: What data needs to be retrieved? (columns, aggregations, calculations)
- **FROM clause**: Which tables are needed?
- **JOIN clauses**: What relationships need to be established?
- **WHERE clause**: What filtering conditions are required?
- **GROUP BY clause**: What grouping is needed for aggregations?
- **HAVING clause**: What post-aggregation filtering is needed?
- **ORDER BY clause**: What sorting is required?
- **LIMIT clause**: Are there any row limits?
- **Subqueries**: Are nested queries needed?
- **Special functions**: Date functions, string functions, mathematical operations

## Step 2: Skeleton (Structured Template)
Create a SQL skeleton with:
- Clear structure showing the logical flow
- Placeholders for table names, column names, and conditions
- Comments explaining the purpose of each section
- Proper indentation and formatting

## Step 3: Complete (Final SQL)
Fill in the skeleton with:
- Exact table and column names from the schema
- Specific values and conditions from the question
- Final validation of the query logic

# Important Rules:
1. **Schema Accuracy**: Use exact table and column names from the provided schema
2. **Logical Flow**: Ensure the query logic matches the question requirements
3. **Performance**: Prefer efficient JOIN patterns over nested subqueries when possible
4. **Readability**: Use clear aliases and proper formatting
5. **Completeness**: Address all aspects mentioned in the question

# Output:
Please respond with:
1. Your comprehensive analysis and planning for the SQL query generation and the SQL skeleton with placeholders, enclosed in ```text``` block.
2. The final SQL query that answers the target question and can be executed by {{ engine }}, enclosed in ```sql``` block.