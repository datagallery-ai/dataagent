from __future__ import annotations

from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.suite.builtin_suites.data_analysis.tools.workflow._workflow_actions import control_workflow


def control_data_analysis_workflow(
    action: str,
    step_id: str = "",
    target: str = "",
    reason: str = "",
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Apply an explicit operator control action to the active DataAnalysis workflow.

    Use this only for uncommon control operations requested by the user or required
    for recovery. Use action="update_step_target" to change a pending or ready step
    before it starts. Use action="silence" to stop the active workflow. Normal
    workflow progression should use advance_data_analysis_workflow instead.

    Args:
        action: Either "update_step_target" or "silence".
        step_id: Step id to update when action is "update_step_target".
        target: New step target when action is "update_step_target".
        reason: Required explanation for either supported action.
    """
    return control_workflow(
        action=action,
        step_id=step_id,
        target=target,
        reason=reason,
        tool_context=_tool_context,
    )
