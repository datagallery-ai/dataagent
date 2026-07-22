# DataAnalysis Workflow Orchestration

When the user asks for a long-running DataAnalysis workflow, use the DataAnalysis workflow tools to keep durable status in the workspace.

## Native Suite Runtime Adaptation

- Before deciding whether to create or advance a workflow, call `inspect_data_analysis_workflow(...)` once to load the durable current state.

- Before deciding whether to create or advance a workflow, call `inspect_data_analysis_workflow(...)` once to load the durable current state.
- Before creating a new workflow, confirm all required inputs are known:
  - the analysis objective and an appropriate `scenario_id`;
  - a non-empty data-source reference is passed as `data_refs`. Existing files in
    the active workspace are automatically published for subagents; database or
    other remote-source references are passed through unchanged;
  - the expected final output, such as ranked entities, SQL, report artifacts, or all of them.
- If any required input is missing, ask the user for the missing information. Do not start a workflow yet.
- If there is no active DataAnalysis workflow and all required inputs are known, call `start_data_analysis_workflow(...)` with `scenario_id`, `data_refs`, and optional `step_targets_json` overrides keyed by scenario step id. For the standard target-audience pipeline, use `scenario_id="target_audience_selection"` so sampling runs before feature engineering; do not skip or manually complete the sampling step.
- While a DataAnalysis workflow is active, use `inspect_data_analysis_workflow(...)` for read-only status checks.
- Use `advance_data_analysis_workflow(...)` only for explicit state transitions:
  - **Hard timeout rule:** every `action="submit_current_step"` call must explicitly pass
    `timeout_sec=3600` or greater. All workflow subagent tasks are complex and long-running; never rely
    on the shorter default timeout. Apply the same minimum timeout when resubmitting a retried step.
  - `action="submit_current_step"` submits a ready current step to its configured subagent. Leave `task` empty to use the persisted step target; change a failed step target through the control tool rather than using a one-off task override.
  - When the matching subagent job reaches any terminal status (`completed`, `failed`, `cancelled`, or `timed_out`), call `action="complete_current_step"` with its `job_id`. The workflow tool is the authoritative job collector: it completes a valid completed job, or records the step as failed.
  - `action="retry_current_step"` only resets a failed current step to ready; pass `retry_reason`, then call `submit_current_step` to create a new subagent job.
- Do not call `submit_subagent(...)` directly while a workflow is active.
- If completion returns `current_step_failed`, the job may have failed or the receipt/artifact acceptance may have failed. Optionally update that failed step target when the recovery requires a changed objective; otherwise retry the current step with `retry_current_step` and submit a new subagent job. Do not invent receipt content.
- A step has at most three retry opportunities after its first submission. If the workflow is silenced because that limit is exhausted, report the final failure reason to the user; do not start another workflow automatically.
- Use `control_data_analysis_workflow(...)` only when the user explicitly changes a `pending`, `ready`, or `failed` step target, asks to stop the workflow, or recovery requires a changed target.
- Do NOT rewrite or update a subagent's step target to specific implementation instructions (e.g., "create table X with columns Y, Z" or "add column A with value B"). The subagent's skill defines the implementation contract; the target should only describe the business objective. If a step fails, prefer `retry_current_step` before updating the target. Update the target only when the business requirement itself has changed, not when you want to give the subagent different implementation directions.
- Orchestration only: do not call `load_skill` for step skills (`feature_engineer`, `model_engineer`, `nl2sql`, `user_sampling`). Those belong to subagents. Keep `step_targets` business-level; do not embed skill script paths, pipeline commands, or execution checklists in targets. Do not prescribe specific table names, column lists, or SQL structures in step targets — the subagent's skill handles those details.
- Do **not** call `submit_resource_job` against ClickHouse (`resource_id="clickhouse"`) on the main agent; database queries belong to step subagents.
- For a completed job, you may verify that `receipt.json` references at least one existing artifact (for example with `bash`/`inspect`), but always call `complete_current_step` to let the workflow tool perform the authoritative acceptance or failure transition.
- You may still use normal execution and file tools for auxiliary checks, intermediate files, commands, or validation. Do not use those tools to bypass workflow status advancement.
- Never directly edit, overwrite, or shell-modify `.metadata/active_workflow.json` or files under `.metadata/workflows/`; these files are owned by the DataAnalysis workflow tools.
