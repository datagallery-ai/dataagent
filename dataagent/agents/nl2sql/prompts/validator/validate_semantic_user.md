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

{% if review_history %}
## Prior Review Rounds
{{ review_history }}
{% endif %}

## Generated SQLs
{{ sqls }}