## Database Schema:
{{ schema }}

## Question:
{{ question }}
{% if evidence %}
## Hints:
{{ evidence }}
{% endif %}

Repeating the question and hint, and generating the SQL with Recursive Divide-and-Conquer approach, and finally try to simplify the SQL query using `INNER JOIN` over nested `SELECT` statements IF POSSIBLE.
