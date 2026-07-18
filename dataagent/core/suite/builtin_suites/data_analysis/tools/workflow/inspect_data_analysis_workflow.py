from __future__ import annotations

from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.suite.builtin_suites.data_analysis.tools.workflow._workflow_actions import inspect_workflow_status


def inspect_data_analysis_workflow(*, _tool_context: ToolExecutionContext) -> dict[str, Any]:
    """Read the current DataAnalysis workflow status without changing it.

    Use this for status checks, planning, or deciding the next user-facing message.
    This tool never submits subagents, completes steps, retries steps, or silences a
    workflow. Use advance_data_analysis_workflow for workflow state transitions.
    """
    return inspect_workflow_status(_tool_context)
