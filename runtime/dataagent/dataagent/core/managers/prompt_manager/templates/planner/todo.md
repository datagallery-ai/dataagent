# Work Plan Status
{% if not has_plan %}
There is **no active work plan** for this session.

Before substantive execution on a **complex** data analysis or multi-step processing task:
1. **Explore** the environment (workspace files, schema, samples) and clarify the user's intent.
2. Call **`create_plan`** with `introduction` (overall goal), `approach` (strategy), and an ordered `todos` list.
3. Then execute **only** what is needed for the first todo—or the exploration step itself if the plan's first todo is exploration.

If the user query is simple and can be answered directly without tools, respond directly and do **not** create a plan.
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