Current state description
{{ current_state }}

Failed trajectory (node and edges of a trajectory, including tool name, parameters, and outcome/success flag)
{{ failed_trajectory }}

New exploration action (the tool call that will be executed next)
Tool name: {{ new_action_name }}
Parameters: {{ new_action_params }}

Based on the above, generate the updated state description as a single coherent paragraph.