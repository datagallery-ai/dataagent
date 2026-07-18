from __future__ import annotations

from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.suite.builtin_suites.data_analysis.tools.workflow._workflow_actions import start_workflow


def start_data_analysis_workflow(
    user_query: str,
    data_refs: str,
    scenario_id: str = "target_audience_selection",
    step_targets_json: str = "",
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Create and activate a scenario-configured DataAnalysis workflow.

    Use this only when the user wants a long-running data analysis workflow, no
    workflow is already active, and the objective plus input data references are
    known. Do not use this to advance an existing workflow; call
    advance_data_analysis_workflow instead.

    Args:
        user_query: The user's analysis objective and expected final output.
        data_refs: Comma or newline separated input paths in the active workspace.
        scenario_id: Scenario YAML id under this Suite's resources directory.
        step_targets_json: Optional JSON object keyed by scenario step id to
            override default step targets.
    """
    return start_workflow(
        user_query=user_query,
        data_refs=data_refs,
        scenario_id=scenario_id,
        step_targets_json=step_targets_json,
        tool_context=_tool_context,
    )
