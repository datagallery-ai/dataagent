1. If a specific column is asked, include ONLY that column in SELECT, nothing more. e.g., if "players sorted by score" is asked, return only players. Score is used only for sorting and should not appear in the result.
2. The result columns MUST include EXACT AND ONLY what the question asks, in the same order. e.g., If the question asks for "total amount and name", return (total amount, name) in that order.
3. If the question refers to a single item (e.g., "the highest one"), return exactly one row.
4. Do not filter NULL unless explicitly required.
5. NEVER concatenate first and last names.
6. When joining multiple tables, use only explicit foreign keys. For example, if TableA → TableB and TableC → TableB, do not join TableA directly with TableC; join TableA → TableB → TableC instead.
