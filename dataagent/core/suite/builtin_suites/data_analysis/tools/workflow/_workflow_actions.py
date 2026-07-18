from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.suite.builtin_suites.data_analysis.tools.workflow._workflow_tools import (
    error,
    runtime_controller,
    submit_agent_job,
    success,
)
from dataagent.core.suite.builtin_suites.data_analysis.tools.workflow.scenario_loader import load_scenario_steps
from dataagent.core.suite.builtin_suites.data_analysis.tools.workflow.status import (
    MAX_STEP_RETRIES,
    STEP_STATUS_FAILED,
    STEP_STATUS_IN_PROGRESS,
    validate_receipt,
)
from dataagent.core.workspace.publish import load_publish_manifest, publish_subagent_artifacts
from dataagent.utils.runtime_paths import (
    is_subagent_output_sharing_enabled,
    resolve_subagent_output_root,
)

RETRYABLE_JOB_STATUSES = frozenset({"failed", "cancelled", "timed_out"})


def start_workflow(
    user_query: str,
    data_refs: str,
    scenario_id: str,
    step_targets_json: str,
    tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    runtime, controller, err = runtime_controller(tool_context)
    if err is not None:
        return err
    assert runtime is not None
    assert controller is not None
    try:
        if controller.load_active_running_workflow() is not None:
            raise ValueError("an active data analysis workflow already exists")
        runtime_config = _runtime_config(runtime)
        if not is_subagent_output_sharing_enabled(runtime_config):
            raise ValueError("Data Analysis requires AGENT_CONFIG.subagent_output_sharing: true")
        input_refs = _parse_data_refs(data_refs)
        input_publication_id = _stage_local_input_refs(
            runtime=runtime,
            input_refs=input_refs,
            config=runtime_config,
        )
        status = controller.create_workflow(
            user_query=user_query,
            input_refs=input_refs,
            steps=load_scenario_steps(scenario_id, step_targets_json),
            shared_input_publication_id=input_publication_id,
        )
    except (OSError, ValueError) as exc:
        return error(exc)
    return success(action="started_workflow", workflow_status=status, message="Data analysis workflow created.")


def inspect_workflow_status(tool_context: ToolExecutionContext) -> dict[str, Any]:
    _, controller, err = runtime_controller(tool_context)
    if err is not None:
        return err
    assert controller is not None
    workflow = controller.load_active_running_workflow()
    if workflow is None:
        return success(
            active=False,
            action="inspected_workflow",
            workflow_status="silent",
            workflow=None,
            next_action="start_data_analysis_workflow",
            message="No active running data analysis workflow.",
        )
    return success(
        active=True,
        action="inspected_workflow",
        workflow_status=workflow.get("status"),
        workflow=workflow,
        next_action="advance_data_analysis_workflow",
        message="Active data analysis workflow loaded.",
    )


def advance_workflow(
    *,
    action: str,
    job_id: str = "",
    retry_reason: str = "",
    task: str = "",
    timeout_sec: int = 600,
    tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    runtime, controller, err = runtime_controller(tool_context)
    if err is not None:
        return err
    assert runtime is not None
    assert controller is not None
    workflow = controller.load_active_running_workflow()
    if workflow is None:
        return success(
            active=False,
            workflow_status="silent",
            workflow=None,
            next_action="start_data_analysis_workflow",
            message="No active running data analysis workflow. Start one when required inputs are known.",
        )
    try:
        current_step = _current_step(workflow)
    except ValueError as exc:
        return error(exc)
    normalized_action = str(action or "").strip()
    step_status = str(current_step.get("status") or "").strip()
    if normalized_action == "submit_current_step":
        if step_status != "ready":
            return error(ValueError(f"current step `{current_step.get('id')}` is not ready"))
        return _submit_ready_step(
            runtime,
            controller,
            workflow,
            current_step,
            tool_context=tool_context,
            task=task,
            timeout_sec=timeout_sec,
        )
    if normalized_action == "complete_current_step":
        if step_status != "in_progress":
            return error(ValueError(f"current step `{current_step.get('id')}` is not in_progress"))
        if not str(job_id or "").strip():
            return error(ValueError("job_id is required for complete_current_step"))
        return _complete_current_step_action(
            runtime,
            controller,
            workflow,
            current_step,
            job_id=job_id,
        )
    if normalized_action == "retry_current_step":
        if step_status == STEP_STATUS_IN_PROGRESS:
            return _recover_legacy_in_progress_step(
                runtime,
                controller,
                workflow,
                current_step,
                retry_reason=retry_reason,
            )
        if step_status != STEP_STATUS_FAILED:
            return error(ValueError(f"current step `{current_step.get('id')}` is not failed"))
        return _retry_current_step_action(runtime, controller, workflow, current_step, retry_reason=retry_reason)
    if not normalized_action:
        return error(ValueError("action is required"))
    return error(ValueError("action must be `submit_current_step`, `complete_current_step`, or `retry_current_step`"))


def _complete_current_step_action(
    runtime: Any,
    controller: Any,
    workflow: dict[str, Any],
    current_step: dict[str, Any],
    *,
    job_id: str,
) -> dict[str, Any]:
    step_job_id = str(current_step.get("job_id") or "").strip()
    requested_job_id = str(job_id or "").strip()
    if requested_job_id != step_job_id:
        return error(ValueError("job_id does not match current step"))

    job_error, job_result = _collect_subagent_job(runtime, requested_job_id)
    if job_error is not None:
        return job_error
    job_status = str(job_result.get("status") or "").strip().lower()
    if job_status != "completed":
        if job_status in RETRYABLE_JOB_STATUSES:
            return _mark_current_step_failed(
                controller,
                job_id=requested_job_id,
                phase="job_execution",
                reason=str(job_result.get("error") or job_result.get("summary") or f"subagent job {job_status}"),
                job_status=job_status,
            )
        return success(
            active=True,
            action="current_step_in_progress",
            job_id=requested_job_id,
            job_status=job_status or "unknown",
            workflow_status=workflow.get("status"),
            workflow=workflow,
            next_action="advance_data_analysis_workflow",
            message=f"Current subagent job is {job_status or 'unknown'}. Complete it after it finishes.",
        )
    return _complete_in_progress_step(
        runtime,
        controller,
        workflow,
        requested_job_id,
        job_result,
    )


def _retry_current_step_action(
    runtime: Any,
    controller: Any,
    workflow: dict[str, Any],
    current_step: dict[str, Any],
    *,
    retry_reason: str,
) -> dict[str, Any]:
    step_job_id = str(current_step.get("job_id") or "").strip()
    if not step_job_id:
        return error(ValueError("current step has no job_id to retry"))
    if not str(retry_reason or "").strip():
        return success(
            active=True,
            action="current_step_failed",
            job_id=step_job_id,
            failure_phase=str(current_step.get("failure_phase") or ""),
            failure_reason=str(current_step.get("failure_reason") or ""),
            workflow_status=workflow,
            workflow=workflow,
            next_action="advance_data_analysis_workflow",
            message="Current workflow step failed. Call advance_data_analysis_workflow with action=retry_current_step and retry_reason.",
        )
    try:
        updated = controller.retry_current_step(reason=retry_reason)
    except ValueError as exc:
        return error(exc)
    return success(
        active=True,
        action="retried_current_step",
        job_id=step_job_id,
        workflow_status=updated,
        next_action="advance_data_analysis_workflow",
        message="Current data analysis workflow step is ready to retry.",
    )


def control_workflow(
    action: str,
    step_id: str = "",
    target: str = "",
    reason: str = "",
    *,
    tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    _, controller, err = runtime_controller(tool_context)
    if err is not None:
        return err
    assert controller is not None
    normalized_action = str(action or "").strip()
    try:
        if normalized_action == "update_step_target":
            if not str(step_id or "").strip():
                raise ValueError("step_id is required for update_step_target")
            if not str(target or "").strip():
                raise ValueError("target is required for update_step_target")
            if not str(reason or "").strip():
                raise ValueError("reason is required for update_step_target")
            status = controller.update_step_target(step_id=step_id, target=target, reason=reason)
            return success(
                action="updated_step_target",
                workflow_status=status,
                message="Data analysis workflow step target updated.",
            )
        if normalized_action == "silence":
            if not str(reason or "").strip():
                raise ValueError("reason is required for silence")
            status = controller.silence_workflow(reason=reason)
            return success(
                action="silenced_workflow",
                workflow_status=status,
                message="Data analysis workflow silenced.",
            )
        raise ValueError("action must be `update_step_target` or `silence`")
    except ValueError as exc:
        return error(exc)


def _submit_ready_step(
    runtime: Any,
    controller: Any,
    workflow: dict[str, Any],
    current_step: dict[str, Any],
    *,
    tool_context: ToolExecutionContext,
    task: str,
    timeout_sec: int,
) -> dict[str, Any]:
    try:
        agent_id = str(current_step.get("owner_id") or "").strip()
        dispatch_task = _dispatch_task(
            workflow,
            current_step,
            workspace_dir=runtime.workspace_dir,
            override_task=str(task or "").strip(),
        )
        result = submit_agent_job(
            runtime,
            tool_context,
            agent_id=agent_id,
            task=dispatch_task,
            timeout_sec=timeout_sec,
        )
        if str(result.get("status") or "").upper() == "ERROR":
            return result
        submitted_job_id = str(result.get("job_id") or "").strip()
        if not submitted_job_id:
            raise ValueError("agent_service.submit did not return job_id")
        updated = controller.mark_current_step_submitted(submitted_job_id)
    except ValueError as exc:
        return error(exc)
    return success(
        action="submitted_current_step",
        active=True,
        job_id=submitted_job_id,
        submit_result=result,
        workflow_status=updated,
    )


def _complete_in_progress_step(
    runtime: Any,
    controller: Any,
    workflow: dict[str, Any],
    job_id: str,
    job_result: dict[str, Any],
) -> dict[str, Any]:
    try:
        published_dir = _published_output_dir(runtime, job_result)
        receipt = _read_job_receipt(published_dir)
    except (OSError, ValueError) as exc:
        return _mark_current_step_failed(
            controller,
            job_id=job_id,
            phase="receipt_validation",
            reason=str(exc),
        )
    try:
        archived_receipt = _map_published_receipt_artifacts(
            runtime=runtime,
            published_dir=published_dir,
            receipt=receipt,
        )
    except (OSError, ValueError) as exc:
        return _mark_current_step_failed(
            controller,
            job_id=job_id,
            phase="artifact_archive",
            reason=str(exc),
        )
    try:
        status = controller.complete_current_step(
            job_id=job_id,
            receipt=archived_receipt,
        )
    except ValueError as exc:
        return error(exc)
    return success(
        active=str(status.get("status") or "") == "running",
        action="completed_current_step",
        workflow_status=status,
        message="Current data analysis workflow step completed.",
    )


def _recover_legacy_in_progress_step(
    runtime: Any,
    controller: Any,
    workflow: dict[str, Any],
    current_step: dict[str, Any],
    *,
    retry_reason: str,
) -> dict[str, Any]:
    job_id = str(current_step.get("job_id") or "").strip()
    if not job_id:
        return error(ValueError("current step has no job_id to retry"))
    job_error, job_result = _collect_subagent_job(runtime, job_id)
    if job_error is not None:
        return job_error
    job_status = str(job_result.get("status") or "").strip().lower()
    if job_status in RETRYABLE_JOB_STATUSES:
        failed = _mark_current_step_failed(
            controller,
            job_id=job_id,
            phase="job_execution",
            reason=str(job_result.get("error") or job_result.get("summary") or f"subagent job {job_status}"),
            job_status=job_status,
        )
    elif job_status == "completed":
        try:
            published_dir = _published_output_dir(runtime, job_result)
            receipt = _read_job_receipt(published_dir)
            for artifact in receipt["artifacts"]:
                if str(artifact.get("kind") or "file") == "file":
                    _resolve_job_artifact_source(str(artifact.get("path") or ""), job_workspace=published_dir)
        except (OSError, ValueError) as exc:
            failed = _mark_current_step_failed(
                controller,
                job_id=job_id,
                phase="receipt_validation",
                reason=str(exc),
                job_status=job_status,
            )
        else:
            return success(
                active=True,
                action="current_step_awaiting_completion",
                job_id=job_id,
                job_status=job_status,
                workflow_status=workflow,
                workflow=workflow,
                next_action="advance_data_analysis_workflow",
                message="Current subagent job completed with a valid receipt. Call advance_data_analysis_workflow with action=complete_current_step.",
            )
    else:
        return success(
            active=True,
            action="current_step_in_progress",
            job_id=job_id,
            job_status=job_status or "unknown",
            workflow_status=workflow,
            workflow=workflow,
            next_action="advance_data_analysis_workflow",
            message=f"Current subagent job is {job_status or 'unknown'}. It cannot be retried yet.",
        )
    if failed.get("action") == "workflow_silenced_retry_limit":
        return failed
    updated_workflow = failed.get("workflow_status")
    failed_step = _current_step(updated_workflow) if isinstance(updated_workflow, dict) else current_step
    return _retry_current_step_action(
        runtime,
        controller,
        updated_workflow if isinstance(updated_workflow, dict) else workflow,
        failed_step,
        retry_reason=retry_reason,
    )


def _mark_current_step_failed(
    controller: Any,
    *,
    job_id: str,
    phase: str,
    reason: str,
    job_status: str = "",
) -> dict[str, Any]:
    try:
        status = controller.mark_current_step_failed(job_id=job_id, phase=phase, reason=reason)
    except ValueError as exc:
        return error(exc)
    silenced = str(status.get("status") or "") == "silent"
    return success(
        active=not silenced,
        action="workflow_silenced_retry_limit" if silenced else "current_step_failed",
        job_id=job_id,
        job_status=job_status,
        failure_phase=phase,
        failure_reason=reason,
        retry_count=_step_retry_count(status, job_id),
        max_retries=MAX_STEP_RETRIES,
        workflow_status=status,
        next_action="" if silenced else "advance_data_analysis_workflow",
        message=(
            "Data analysis workflow was silenced because the current step exhausted its retry limit."
            if silenced
            else (
                f"Current workflow step failed: {reason}. "
                "Call advance_data_analysis_workflow with action=retry_current_step and retry_reason."
            )
        ),
    )


def _step_retry_count(status: dict[str, Any], job_id: str) -> int:
    for step in status.get("steps", []):
        if isinstance(step, dict) and str(step.get("job_id") or "").strip() == str(job_id or "").strip():
            return int(step.get("retry_count") or 0)
    return 0


def _collect_subagent_job(runtime: Any, job_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    agent_service = runtime.ensure_job_services()
    if agent_service is None:
        return {"status": "ERROR", "message": "runtime agent_service.collect is unavailable."}, {}
    result = agent_service.collect(job_id=job_id)
    if not isinstance(result, dict):
        return {"status": "ERROR", "message": "unable to read subagent job status"}, {}
    return None, result


def _parse_data_refs(raw: str) -> list[str]:
    refs: list[str] = []
    for chunk in str(raw or "").replace("\n", ",").split(","):
        ref = chunk.strip()
        if ref:
            refs.append(ref)
    if not refs:
        raise ValueError("data_refs is required")
    return refs


def _current_step(status: dict[str, Any]) -> dict[str, Any]:
    current_step_id = str(status.get("current_step_id") or "").strip()
    for step in status.get("steps", []):
        if isinstance(step, dict) and step.get("id") == current_step_id:
            return step
    raise ValueError("workflow current step is unavailable")


def _dispatch_task(
    status: dict[str, Any],
    current_step: dict[str, Any],
    *,
    workspace_dir: str | Path,
    override_task: str = "",
) -> str:
    base = override_task or str(current_step.get("target") or "").strip()
    lines = [base]
    input_publication_id = str(status.get("shared_input_publication_id") or "").strip()
    if input_publication_id:
        lines.extend(
            [
                "",
                "Local workflow inputs are published in the shared output manifest.",
                f"Select the entry with subagent_id `{input_publication_id}` before reading local input files.",
            ]
        )
    lines.extend(
        [
            "",
            "Write artifacts in the current subagent workspace.",
            "Write a receipt.json file in the current subagent workspace.",
        ]
    )
    previous_receipt = _previous_step_receipt(status, str(current_step.get("id") or ""))
    previous_summary = str(previous_receipt.get("summary") or "").strip()
    previous_artifacts = [item for item in previous_receipt.get("artifacts", []) if isinstance(item, dict)]
    if previous_summary:
        lines.extend(["", "Previous step summary:", previous_summary])
    if previous_artifacts:
        lines.extend(["", "Previous step artifacts:"])
        has_clickhouse_table = False
        for artifact in previous_artifacts:
            if str(artifact.get("kind") or "file") == "clickhouse_table":
                has_clickhouse_table = True
                uri = str(artifact.get("uri") or "").strip()
                name = str(artifact.get("name") or "").strip()
                if uri and name:
                    lines.append(f"- ClickHouse table: {name} ({uri})")
                elif uri:
                    lines.append(f"- ClickHouse table: {uri}")
                continue
            artifact_path = str(artifact.get("path") or "").strip()
            artifact_type = str(artifact.get("type") or "").strip()
            if artifact_path:
                label = f" ({artifact_type})" if artifact_type else ""
                lines.append(f"- published local artifact{label}: {artifact_path}")
        if has_clickhouse_table:
            lines.append(
                "Read listed ClickHouse tables through the ClickHouse resource; do not treat their URIs as local files."
            )
    return "\n".join(lines).strip()


def _runtime_config(runtime: Any) -> dict[str, Any] | None:
    get_all_config = getattr(runtime, "get_all_config", None)
    config = get_all_config() if callable(get_all_config) else None
    return config if isinstance(config, dict) else None


def _stage_local_input_refs(*, runtime: Any, input_refs: list[str], config: dict[str, Any] | None) -> str:
    workspace_dir = Path(getattr(runtime, "workspace_dir", "")).expanduser().resolve()
    local_sources: list[Path] = []
    for ref in input_refs:
        if "://" in ref:
            continue
        candidate = Path(ref).expanduser()
        candidate = candidate.resolve() if candidate.is_absolute() else (workspace_dir / candidate).resolve()
        try:
            candidate.relative_to(workspace_dir)
        except ValueError as exc:
            raise ValueError(f"local data reference is outside active workspace: {ref}") from exc
        if not candidate.is_file():
            raise ValueError(f"local data reference is not a file: {ref}")
        local_sources.append(candidate)
    if not local_sources:
        return ""

    publication_id = f"workflow-input-{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="data-analysis-inputs-", dir=workspace_dir) as staging_root:
        staging_dir = Path(staging_root)
        for index, source in enumerate(local_sources, start=1):
            shutil.copy2(source, staging_dir / f"{index:02d}_{source.name}")
        publish_subagent_artifacts(
            source_workspace=staging_dir,
            parent_workspace=workspace_dir,
            subagent_session_id=publication_id,
            agent_id="data_analysis_workflow",
            task="stage local workflow inputs",
            job_id=publication_id,
            config=config,
        )
    return publication_id


def _previous_step_receipt(status: dict[str, Any], current_step_id: str) -> dict[str, Any]:
    steps = [item for item in status.get("steps", []) if isinstance(item, dict)]
    for index, step in enumerate(steps):
        if step.get("id") != current_step_id:
            continue
        if index <= 0:
            return {"summary": "", "artifacts": []}
        receipt = steps[index - 1].get("receipt")
        return receipt if isinstance(receipt, dict) else {"summary": "", "artifacts": []}
    return {"summary": "", "artifacts": []}


def _read_job_receipt(job_workspace: Path) -> dict[str, Any]:
    receipt_path = job_workspace / "receipt.json"
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"receipt.json not found in current subagent job workspace: {receipt_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("receipt.json must contain valid JSON") from exc
    return _validate_job_receipt(payload)


def _validate_job_receipt(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("receipt.json must contain a JSON object")
    return validate_receipt(payload)


def _map_published_receipt_artifacts(
    *,
    runtime: Any,
    published_dir: Path,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    workspace_dir = Path(getattr(runtime, "workspace_dir", "")).resolve()
    mapped_artifacts: list[dict[str, str]] = []
    for artifact in receipt.get("artifacts", []):
        if str(artifact.get("kind") or "file") == "clickhouse_table":
            mapped_artifacts.append(
                {
                    "kind": "clickhouse_table",
                    "uri": str(artifact.get("uri") or "").strip(),
                    "name": str(artifact.get("name") or "").strip(),
                }
            )
            continue
        raw_path = str(artifact.get("path") or "").strip()
        source = _resolve_job_artifact_source(raw_path, job_workspace=published_dir)
        try:
            relative_path = source.relative_to(workspace_dir).as_posix()
        except ValueError as exc:
            raise ValueError(f"published artifact is outside the parent workspace: {raw_path}") from exc
        mapped_artifacts.append(
            {
                "path": relative_path,
                "type": str(artifact.get("type") or "").strip() or _artifact_type_from_path(raw_path),
            }
        )
    return {"summary": str(receipt.get("summary") or "").strip(), "artifacts": mapped_artifacts}


def _published_output_dir(runtime: Any, job_result: dict[str, Any]) -> Path:
    workspace_dir = Path(getattr(runtime, "workspace_dir", "")).expanduser().resolve()
    raw_path = str(job_result.get("published_path") or "").strip()
    if not raw_path:
        raise ValueError("subagent result does not contain published_path")
    published_dir = Path(raw_path).expanduser()
    if not published_dir.is_absolute():
        raise ValueError("subagent published_path must be absolute")
    published_dir = published_dir.resolve()
    shared_root = resolve_subagent_output_root(parent_workspace=workspace_dir, config=_runtime_config(runtime))
    try:
        published_dir.relative_to(shared_root)
    except ValueError as exc:
        raise ValueError("subagent published_path is outside the shared output directory") from exc
    if not published_dir.is_dir():
        raise ValueError("subagent published_path is not a directory")
    session_id = str(job_result.get("subagent_session_id") or "").strip()
    job_id = str(job_result.get("job_id") or "").strip()
    if not session_id or not job_id:
        raise ValueError("subagent result does not contain publication identity")
    if published_dir != (shared_root / session_id).resolve():
        raise ValueError("subagent published_path does not match its session id")
    entries = load_publish_manifest(shared_root).get("entries", [])
    matching_entry = next(
        (
            entry
            for entry in entries
            if isinstance(entry, dict)
            and str(entry.get("subagent_id") or "").strip() == session_id
            and str(entry.get("job_id") or "").strip() == job_id
        ),
        None,
    )
    if not isinstance(matching_entry, dict):
        raise ValueError("subagent publication is missing from the shared output manifest")
    manifest_path = Path(str(matching_entry.get("published_path") or "")).expanduser().resolve()
    if manifest_path != published_dir:
        raise ValueError("subagent published_path does not match the shared output manifest")
    return published_dir


def _resolve_job_artifact_source(raw_path: str, *, job_workspace: Path) -> Path:
    text = str(raw_path or "").strip()
    if not text:
        raise ValueError("artifact path is required")
    path = Path(text)
    candidate = path.resolve() if path.is_absolute() else (job_workspace / path).resolve()
    try:
        candidate.relative_to(job_workspace.resolve())
    except ValueError as exc:
        raise ValueError(f"artifact path is outside job workspace: {raw_path}") from exc
    if not candidate.is_file():
        raise ValueError(f"artifact file not found: {raw_path}")
    return candidate


def _artifact_type_from_path(path: str) -> str:
    return Path(str(path or "")).suffix.lower().lstrip(".") or "file"
