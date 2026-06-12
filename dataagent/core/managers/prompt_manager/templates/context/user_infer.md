Here is the current agent context:

{% if user_query %}
<query>
{{ user_query }}
</query>
{% endif %}

{% if not past_state %}
<history_context>
{% if enable_summary and history_context %}
{{ history_context }}
{% else %}
No historical context available.
{% endif %}
</history_context>
{% endif %}

{% if past_state %}
<past_state>
{{ past_state }}
</past_state>
{% endif %}

<past_action>
{{ past_action }}
</past_action>

<available_actions>
{{ available_actions }}
</available_actions>

{% if enable_ir_unpack and data_lineage %}
<data_lineage>
{{ data_lineage }}
</data_lineage>
{% endif %}

{% if enable_ir_unpack %}
Produce:
1. The `<perfect_state_space>` JSON block.
2. The `<unpack_data_ir>` list block.
{% else %}
Produce the `<perfect_state_space>` JSON block only.
{% endif %}