from __future__ import annotations

from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.suite.builtin_suites.data_analysis.tools.workflow._workflow_actions import advance_workflow


def advance_data_analysis_workflow(
    action: str,
    job_id: str = "",
    retry_reason: str = "",
    task: str = "",
    timeout_sec: int = 600,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Advance the active DataAnalysis workflow using an explicit state-transition action.

    Use action="submit_current_step" only for a ready current step. Use
    action="complete_current_step" after the current step's subagent job reaches
    a terminal status; it completes successful jobs and records failed jobs or
    invalid receipts as failed workflow steps. Use
    action="retry_current_step" only when the current step is failed. A step can
    fail because its job failed, was cancelled, timed out, or because receipt
    validation/archival failed. For a read-only status check, use
    inspect_data_analysis_workflow instead. Do not call submit_subagent directly
    while a workflow is active.

    Args:
        action: One of "submit_current_step", "complete_current_step", or
            "retry_current_step".
        job_id: Optional current step subagent job id. Required when completing a
            finished step; otherwise the current step job is used.
        retry_reason: Reason for retrying a failed current step.
        task: Optional override instruction when submitting a ready step. Leave
            empty to use the scenario-configured step target.
        timeout_sec: Maximum seconds for a newly submitted subagent job.
    """
    return advance_workflow(
        action=action,
        job_id=job_id,
        retry_reason=retry_reason,
        task=task,
        timeout_sec=timeout_sec,
        tool_context=_tool_context,
    )
