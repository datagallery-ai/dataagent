Current perfect state space (JSON object with keys "goal_intent", "belief_about_world", "action_history_summary", "current_position", "available_actions", "user_feedback_state", "epistemic_state"):
{{ current_state }}

Failed trajectory (nodes and edges of a trajectory, including tool name, parameters, and outcome/success flag):
{{ failed_trajectory }}

New exploration action (the tool call that will be executed next):
{{ new_actions }}

Based on the above inputs, generate the UPDATED perfect state space as a single JSON object that:
- Uses EXACTLY the seven keys: "goal_intent", "belief_about_world", "action_history_summary", "current_position", "available_actions", "user_feedback_state", "epistemic_state".
- Updates each field in the same language as given, as coherent first-person paragraphs, following the requirements in the system instructions.
- Does not include any extra keys or any text outside of that single JSON object.