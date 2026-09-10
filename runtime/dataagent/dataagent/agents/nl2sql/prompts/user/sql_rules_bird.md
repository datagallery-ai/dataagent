1. **Thorough Question Analysis**: Address all conditions mentioned in the question.
2. **Strictly Follow Hints**: Adhere to all provided hints.
3. **Formatting**: Pay attention to formatting requirements in the question (e.g., decimal places, percentage). Follow the Hints for specific formatting formulas.
4. **Schema Accuracy**: Use exact table and column names from the provided schema.
5. **SELECT Clause**: Only select columns mentioned in the user's question and with the SAME ORDER as the question requires. Avoid unnecessary columns or values.
6. **Column Selection**: Carefully analyze column descriptions and hints to choose the correct column when similar columns exist across tables.
7. **Value Matching**: "Matched values" and "Examples" in column comments provide values similar to key phrases in the question, helping identify relevant tables and columns.
8. **FROM/JOIN Clauses**: Only include tables essential to answer the question.
9. **Foreign Key Constraints**: Only JOIN tables with explicit foreign key relationships. For example, if TableA → TableB and TableC → TableB, join through TableB instead of directly joining TableA and TableC.
10. **Handling NULLs**: If a column may contain NULL values, use `WHERE <column> IS NOT NULL`.
11. **DISTINCT Keyword**: Use `SELECT DISTINCT` when the question requires unique values.
12. **String Concatenation**: Never use `|| ' ' ||` or any other method to concatenate strings in the `SELECT` clause.
13. **SQLite Functions Only**: Use only SQLite-compatible functions and syntax.
14. **Date Processing**: Prefer utilize `STRFTIME()` for date manipulation (e.g., `STRFTIME('%Y', SOMETIME)` to extract the year).
15. **Schema Syntax**: When table name or column name contains whitespace, include quotes (`table_name` or `column_name`) around the table name or column name.
16. **Performance**: Prefer efficient JOIN patterns over nested subqueries when possible.