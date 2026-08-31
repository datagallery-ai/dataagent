from __future__ import annotations

from pathlib import Path
from typing import Any

from dataagent.core.suite.builtin_suites.data_analysis.tools.workflow.status import (
    DataAnalysisWorkflowController,
)
from dataagent.governance import GovernanceInvocation


def data_analysis_orchestration_policy(inv: GovernanceInvocation) -> None:
    """Keep main-agent writes and dispatches inside the workflow controller."""
    runtime = inv.runtime
    if str(getattr(runtime, "hierarchy", "") or "").upper() != "MAIN":
        return
    if not data_analysis_orchestration_active(runtime):
        return
    if inv.tool_name == "submit_subagent":
        raise ValueError(
            "Active data analysis workflows must dispatch through "
            "advance_data_analysis_workflow(action='submit_current_step')."
        )
    if _is_direct_workflow_state_write(inv.tool_name, inv.tool_args, runtime):
        raise ValueError(
            "Data analysis workflow state is controller-owned; use workflow tools instead of editing metadata."
        )


def _is_direct_workflow_state_write(tool_name: str, tool_args: dict[str, Any], runtime: Any) -> bool:
    if tool_name in {"edit_file", "write_file"}:
        return _is_protected_workflow_state_path(str((tool_args or {}).get("path") or ""), runtime)
    if tool_name == "bash":
        return _command_mentions_protected_workflow_state(str((tool_args or {}).get("command") or ""), runtime)
    return False


def _is_protected_workflow_state_path(raw_path: str, runtime: Any) -> bool:
    raw = str(raw_path or "").strip()
    workspace_dir = getattr(runtime, "workspace_dir", None)
    if not raw or workspace_dir is None:
        return False
    workspace = Path(workspace_dir).resolve()
    path = Path(raw)
    candidate = (path if path.is_absolute() else workspace / path).resolve()
    metadata = workspace / ".metadata"
    return candidate == metadata / "active_workflow.json" or _is_relative_to(candidate, metadata / "workflows")


def _command_mentions_protected_workflow_state(command: str, runtime: Any) -> bool:
    if ".metadata/workflows" in command or "active_workflow.json" in command:
        return True
    workspace_dir = getattr(runtime, "workspace_dir", None)
    if workspace_dir is None:
        return False
    workspace = Path(workspace_dir).resolve().as_posix()
    return f"{workspace}/.metadata/workflows" in command or f"{workspace}/.metadata/active_workflow.json" in command


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def data_analysis_orchestration_active(runtime: Any) -> bool:
    """Return whether the workspace has an active running data analysis workflow."""
    workspace_dir = getattr(runtime, "workspace_dir", None)
    if workspace_dir is None:
        return False
    return DataAnalysisWorkflowController(workspace_dir).load_active_running_workflow() is not None
