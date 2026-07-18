from __future__ import annotations

from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.jobs.envelope import envelope_from_tool_context
from dataagent.core.suite.builtin_suites.data_analysis.tools.workflow.status import (
    DataAnalysisWorkflowController,
)


def runtime_controller(
    tool_context: ToolExecutionContext,
) -> tuple[Any | None, DataAnalysisWorkflowController | None, dict[str, Any] | None]:
    runtime = tool_context.runtime
    if runtime is None:
        return None, None, {"status": "ERROR", "message": "data analysis workflow tools require a mounted runtime."}
    workspace_dir = getattr(runtime, "workspace_dir", None)
    if workspace_dir is None:
        return runtime, None, {"status": "ERROR", "message": "runtime workspace_dir is unavailable."}
    return runtime, DataAnalysisWorkflowController(workspace_dir), None


def success(**payload: Any) -> dict[str, Any]:
    return {"status": "SUCCESS", **payload}


def error(exc: Exception) -> dict[str, Any]:
    return {"status": "ERROR", "message": str(exc)}


def submit_agent_job(
    runtime: Any,
    tool_context: ToolExecutionContext,
    agent_id: str,
    task: str,
    timeout_sec: int,
) -> dict[str, Any]:
    agent_service = runtime.ensure_job_services()
    if agent_service is None:
        return {"status": "ERROR", "message": "runtime agent service is unavailable."}
    return agent_service.submit(
        agent_id=agent_id,
        task=task,
        timeout_sec=int(timeout_sec),
        job_envelope=envelope_from_tool_context(tool_context) or None,
    )
