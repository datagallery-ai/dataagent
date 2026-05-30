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

## Generated SQLs
{{ sqls }}