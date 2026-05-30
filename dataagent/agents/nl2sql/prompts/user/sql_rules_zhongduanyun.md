1. Joins are not provided and should be inferred based on shared keys and common data patterns.
2. When selecting from a table, always include the partition filter (e.g. `pt_d` = '$date')
3. Never use `DISTINCT`. Use `GROUP BY` instead.