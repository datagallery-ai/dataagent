## Database Schema:
{{ schema }}

{% if evidence %}
## Evidence:
{{ evidence }}
{% endif %}

## Question:
{{ question }}

{% if sql_rules %}
## Additional Rules:
{{ sql_rules }}
{% endif %}

Repeating the question, and generating the SQL with Recursive Divide-and-Conquer approach, and finally try to simplify the SQL query using `INNER JOIN` over nested `SELECT` statements IF POSSIBLE.
