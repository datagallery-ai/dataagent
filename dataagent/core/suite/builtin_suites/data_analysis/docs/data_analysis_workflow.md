# Data Analysis workflow

The Suite stores durable workflow state under `.metadata/workflows/<run_id>/`.
Accepted local artifacts are published under `subagent_output/<session>/`.
`resources/target_audience_selection.yaml` defines the default four-step flow.

The main Agent must inspect durable state before starting or advancing a flow.
Each ready step is submitted through `advance_data_analysis_workflow`; successful
children return `receipt.json` from their own `subagents/<session>` workspace.
The controller validates the published artifacts before advancing the next step.
On retry, the main Agent may discover a prior workspace with `search_workspaces`
/ `inspect_workspace` and pass `workspace_rel_path` into
`advance_data_analysis_workflow(action="submit_current_step", ...)` to reuse it;
omit the path to allocate a fresh workspace.

With `AGENT_CONFIG.subagent_output_sharing: true`, completed children
also publish business artifacts to `subagent_output/<session>/`. The workflow
stages local input files there before the first step; subsequent children use
the read-only `subagent_output/manifest.json` index rather than reading the
parent workspace or peer workspaces.

Dynamic Python prompts, runtime-mount initialization, and resource-job post-hooks
from the legacy 5519 plugin are intentionally not part of this native Suite.
