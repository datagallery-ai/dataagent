# User Query
<user_query>{{ user_query }}</user_query>
{% if database_context %}

# Database Context

The current task includes available database context.
When the user query involves data retrieval, SQL generation, table analysis, or database-related planning, incorporate the following database information into your reasoning and planning.

{{ database_context }}
{% endif %}
{% if planning_instructions %}
# Task Constraints

These constraints are **MANDATORY** for the current task. Follow them in order; do not skip planning steps they require.
{{ planning_instructions }}
{% endif %}
{% if memory %}
# User Memory

{{ memory }}
{% endif %}
{% if user_prompt_append %}
# Additional User Context

{{ user_prompt_append }}
{% endif %}
# General Requirements

- Reason and respond in the same language as the user query.
- Default to starting with useful user-facing content rather than task classification, tool policy, or internal process narration.
- If the task clearly needs clarification, uncertainty disclosure, or a safety/risk note before answering, keep that brief and relevant.

# Working Directory

Use absolute paths for workspace files under <working_directory>{{ working_directory }}</working_directory>.
Reuse resolved paths returned by tools when referring to the same file later.
{% if allow_path_lines %}
You may read under these additional absolute read-only roots:

{{ allow_path_lines }}

{% endif %}
If the system message includes a **Skills** section, you may read the listed `skill/<name>/...` assets. Otherwise, do not use skill paths.
