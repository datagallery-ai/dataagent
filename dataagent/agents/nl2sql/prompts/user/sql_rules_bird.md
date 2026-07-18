1. ONLY SELECT the columns asked in the question, and in the SAME ORDER as the question requires. Avoid unnecessary columns. Columns used only for sorting, filtering, joining, or calculation MUST NOT appear in SELECT.
2. MUST include EXACT AND ONLY the conditions and operations explicitly stated in the question.
3. For ORDER BY, MIN/MAX, or division, MUST exclude NULL values; otherwise do not filter NULL unless explicitly required.
4. If a metric, formula, operator, date range, or LIKE pattern is defined in the question, MUST follow it exactly.
5. For date manipulation, use STRFTIME().
6. NEVER use `|| ' ' ||` or any other method to concatenate strings.