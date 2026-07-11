# Work Plan Status
{% if not has_plan %}
{% if skill_md_read_without_plan or (tool_call_count and tool_call_count >= plan_required_threshold) %}
⚠️ **PLAN REQUIRED — {{ tool_call_count }} tool call(s) made without an active plan{% if skill_md_read_without_plan %}, and a skill's SKILL.md has been read{% endif %}**.

This is a strong signal that the task is complex (multi-step / data-dependent / skill-driven). Continuing without a plan risks:
- redoing already-completed steps (no `complete_current_todo` tracking)
- losing state across rounds (no todo list to anchor attention)
- triggering redundant HITL on already-confirmed actions

**For this turn, call `create_plan` FIRST** (unless the answer can now be given in one short turn without further tools). If a skill's SKILL.md is in context, register its `## Workflow` steps as the plan's `todos`.
{% else %}
There is **no active work plan** for this session.

Before substantive execution on a **complex** data analysis or multi-step processing task:
1. **Explore** the environment (workspace files, schema, samples) and clarify the user's intent.
2. Call **`create_plan`** with `introduction` (overall goal), `approach` (strategy), and an ordered `todos` list.
3. Then execute **only** what is needed for the first todo—or the exploration step itself if the plan's first todo is exploration.

If the user query is simple and can be answered directly without tools, respond directly and do **not** create a plan.
{% endif %}
{% elif plan_all_todos_done %}

**Overall task (introduction):** {{ plan_introduction }}

**Approach:** {{ plan_approach }}

**Todo progress:**
{{ plan_todos_overview }}

All todos in the current plan are **completed**.

For this turn:
- **Summarize** what was accomplished and deliver the user's requested outcome; or
- If the user goal is not fully satisfied, call **`create_plan`** or **`update_plan`** to define the **next** phase, then continue.
- Call **`delete_plan`** only if the plan should be discarded entirely.
{% else %}

**Overall task (introduction):** {{ plan_introduction }}

**Approach:** {{ plan_approach }}

**Current todo (execute this step now):** {{ plan_current_todo }}

**Full todo list:**
{{ plan_todos_overview }}

Focus this turn on completing **only** the current todo. When that step's work is done, call **`complete_current_todo`** before starting the next item. Do not skip ahead or redo completed todos unless the user or new evidence requires **`update_plan`**.
{% endif %}