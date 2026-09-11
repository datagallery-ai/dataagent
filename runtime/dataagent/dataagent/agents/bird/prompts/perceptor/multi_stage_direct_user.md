## Database Schema:
{{ schema }}

## Question:
{{ question }}

{% if evidence %}
## Evidence:
{{ evidence }}
{% endif %}

Return a fenced JSON object mapping selected tables to exact column names:
```json
{"selection": {"table_name": ["column_name"]}}
```
