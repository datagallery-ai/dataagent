# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Runner helpers for resource job execution (local sandbox and MCP poll loops)."""

from __future__ import annotations

import time
from collections.abc import Callable
from threading import Event
from typing import Any

from loguru import logger

from dataagent.core.jobs.models import JobResult
from dataagent.core.resource_runtime.operations.operations import ResourceOperationContext
from dataagent.resources.catalog.models import Resource
from dataagent.resources.resolve.prepare import DriverBinding, SubmitPlan

_REMOTE_TERMINAL_STATUSES = frozenset({"completed", "success", "succeeded", "failed", "error", "cancelled", "canceled"})


def build_resource_runner(
    *,
    coordinator: Any,
    resource: Resource,
    envelope: dict[str, Any],
    plan: SubmitPlan,
) -> Callable[[str, Event], JobResult]:
    """Build a JobService runner that executes one resource job and releases capacity.

    Args:
        coordinator: :class:`ResourceJobCoordinator` owning capacity and services.
        resource: Selected executable resource.
        envelope: Finalized submit envelope.
        plan: Prepared submit plan from :class:`ResourceResolve`.

    Returns:
        Runner callable accepted by :meth:`JobService.start`.
    """

    def runner(job_id: str, cancel_event: Event) -> JobResult:
        try:
            if cancel_event.is_set():
                return JobResult(
                    job_id=job_id,
                    agent_id=plan.agent_id,
                    status="cancelled",
                    summary="Resource job cancelled.",
                )
            result = run_resource_operations(
                coordinator=coordinator,
                resource=resource,
                envelope=envelope,
                plan=plan,
                cancel_event=cancel_event,
            )
            status = job_status(result, cancelled=cancel_event.is_set())
            exit_code = int(result.get("exit_code", 0 if status == "completed" else 1))
            return JobResult(
                job_id=job_id,
                agent_id=plan.agent_id,
                status=status,
                summary=summary_text(result)[:2000],
                outputs=outputs(result),
                metrics={
                    "exit_code": exit_code,
                    **(result.get("metrics") if isinstance(result.get("metrics"), dict) else {}),
                },
                error=error_text(result) if status == "failed" else "",
                log_file_info=_extract_log_file_info(result),
            )
        finally:
            coordinator.release_capacity(job_id)

    return runner


def run_resource_operations(
    *,
    coordinator: Any,
    resource: Resource,
    envelope: dict[str, Any],
    plan: SubmitPlan,
    cancel_event: Event,
) -> dict[str, Any]:
    """Execute submit/poll/collect operations for one resource job."""
    resource_id = resource.metadata.get("name") or resource.metadata.get("id", "unknown")
    logger.debug(f"[runner] start run_resource_operations resource={resource_id}")
    context = ResourceOperationContext(runtime=coordinator.runtime, resource=resource, cancel_event=cancel_event)
    allocation = plan.allocation.get("resource") if isinstance(plan.allocation.get("resource"), dict) else {}
    submit_result = invoke_operation(
        coordinator=coordinator,
        resource=resource,
        driver=plan.driver,
        operation="submit",
        arguments={"envelope": envelope, "allocation": allocation},
        context=context,
    )
    logger.debug(f"[runner] submit done resource={resource_id} result={submit_result}")
    if not isinstance(submit_result, dict):
        submit_result = {"status": "completed", "result": submit_result}
    remote_job_id = remote_job_id_from_payload(submit_result)
    status = str(submit_result.get("status") or "").strip().lower()
    final_result = submit_result

    if remote_job_id and status not in _REMOTE_TERMINAL_STATUSES:
        deadline = time.monotonic() + max(1, int(envelope.get("timeout_sec") or 3600))
        logger.debug(
            f"[runner] start polling resource={resource_id} job_id={remote_job_id}"
            f" timeout_sec={envelope.get('timeout_sec')} deadline={deadline:.0f}"
        )
        cursor: str | None = None
        poll_count = 0
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                logger.debug(f"[runner] cancel requested resource={resource_id} job_id={remote_job_id}")
                cancel_operation(coordinator, resource, plan.driver, remote_job_id, context)
                return {"status": "cancelled", "exit_code": 130, "error": "cancelled"}
            poll_result = invoke_operation(
                coordinator=coordinator,
                resource=resource,
                driver=plan.driver,
                operation="poll",
                arguments={"job_id": remote_job_id, "cursor": cursor, "event_limit": 20},
                context=context,
            )
            poll_count += 1
            final_result = poll_result if isinstance(poll_result, dict) else {"result": poll_result}
            status = str(final_result.get("status") or "").strip().lower()
            cursor = str(final_result.get("cursor") or cursor or "") or None
            logger.debug(
                f"[runner] poll #{poll_count} resource={resource_id} job_id={remote_job_id}"
                f" status={status} cursor={cursor}"
            )
            if status in _REMOTE_TERMINAL_STATUSES:
                break
            time.sleep(3.0)
        else:
            logger.debug(f"[runner] poll timeout resource={resource_id} job_id={remote_job_id} poll_count={poll_count}")
            cancel_operation(coordinator, resource, plan.driver, remote_job_id, context)
            return {"status": "failed", "exit_code": 1, "error": "resource job timed out"}

    if remote_job_id and status in _REMOTE_TERMINAL_STATUSES:
        logger.debug(f"[runner] start collect resource={resource_id} job_id={remote_job_id}")
        collected = invoke_operation(
            coordinator=coordinator,
            resource=resource,
            driver=plan.driver,
            operation="collect",
            arguments={"job_id": remote_job_id},
            context=context,
        )
        if isinstance(collected, dict):
            final_result = merge_remote_terminal_result(final_result, collected)
    logger.debug(
        f"[runner] done resource={resource_id} final_status={status} "
        f"result_keys={list(final_result.keys()) if isinstance(final_result, dict) else 'N/A'}"
    )
    return final_result


def invoke_operation(
    *,
    coordinator: Any,
    resource: Resource,
    driver: DriverBinding,
    operation: str,
    arguments: dict[str, Any],
    context: ResourceOperationContext,
) -> Any:
    """Invoke one configured operation for a resource."""
    resource_id = resource.metadata.get("name") or resource.metadata.get("id", "unknown")
    operation_id = str(driver.operation_ids.get(operation) or "").strip()
    logger.debug(
        f"[runner] invoke_operation resource={resource_id} operation={operation} operation_id={operation_id} "
        f"driver={driver.transport_type} args_keys={list(arguments.keys())}"
    )
    if driver.transport_type == "mcp":
        client = coordinator.get_mcp_client(resource.id, driver)
        result = client.call_tool_sync(operation_id, arguments)
        logger.debug(
            f"[runner] invoke_operation done resource={resource_id} operation={operation}"
            f"result_type={type(result).__name__}"
            f"result_keys={list(result.keys()) if isinstance(result, dict) else 'N/A'}"
        )
        return result
    result = coordinator.operation_registry.invoke(operation_id, arguments, context)
    logger.debug(
        f"[runner] invoke_operation done resource={resource_id}"
        f"operation={operation} result_type={type(result).__name__}"
    )
    return result


def cancel_operation(
    coordinator: Any,
    resource: Resource,
    driver: DriverBinding,
    job_id: str,
    context: ResourceOperationContext,
) -> None:
    """Best-effort cancel hook for remote resource backends."""
    try:
        invoke_operation(
            coordinator=coordinator,
            resource=resource,
            driver=driver,
            operation="cancel",
            arguments={"job_id": job_id},
            context=context,
        )
    except Exception as exc:
        logger.warning("Best-effort resource cancel failed for job {}: {}", job_id, exc)


def remote_job_id_from_payload(payload: Any) -> str:
    """Extract a remote backend job id from an operation result."""
    if not isinstance(payload, dict):
        return ""
    for key in ("job_id", "remote_job_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def merge_remote_terminal_result(
    poll_result: dict[str, Any],
    collect_result: dict[str, Any],
) -> dict[str, Any]:
    """Merge poll/submit and collect payloads, preferring collect error details."""
    merged = dict(poll_result)
    for key, value in collect_result.items():
        if value is None:
            continue
        if key == "outputs" and isinstance(value, list) and value:
            merged[key] = value
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if key in {"error", "summary"}:
            merged[key] = value
            continue
        if key not in merged or merged.get(key) in (None, "", [], {}):
            merged[key] = value
    collect_status = str(collect_result.get("status") or "").strip().lower()
    if collect_status:
        merged["status"] = collect_result.get("status")
    if collect_result.get("exit_code") is not None:
        merged["exit_code"] = collect_result.get("exit_code")
    return merged


def job_status(payload: dict[str, Any], *, cancelled: bool) -> str:
    """Map an operation result payload to a terminal job status."""
    if cancelled:
        return "cancelled"
    status = str(payload.get("status") or "").strip().lower()
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status in {"failed", "error"} or int(payload.get("exit_code", 0) or 0) != 0:
        return "failed"
    return "completed"


def summary_text(payload: Any) -> str:
    """Extract a human-readable summary from an operation result."""
    if isinstance(payload, dict):
        for key in ("summary", "message", "stdout", "result"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return str(payload)
    return str(payload or "")


def error_text(payload: dict[str, Any]) -> str:
    """Extract an error string from an operation result."""
    return str(payload.get("error") or payload.get("stderr") or summary_text(payload))


def _extract_log_file_info(payload: Any) -> dict[str, Any] | None:
    """Pull the OBS ``logFileInfo`` block out of a MCP result payload.

    Some MCP servers (e.g. DataOps) expose a pre-signed log download URL under
    ``logFileInfo`` / ``log_file_info`` when a job fails. Persist the block on
    :class:`JobResult` so downstream ``collect`` callers can fetch the log
    without re-querying the remote backend.
    """
    if not isinstance(payload, dict):
        return None
    info = payload.get("logFileInfo")
    if not isinstance(info, dict) or not info:
        info = payload.get("log_file_info")
    if not isinstance(info, dict) or not info:
        return None
    return dict(info)


def outputs(payload: Any) -> list[dict[str, Any]]:
    """Normalize structured outputs from an operation result."""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("outputs")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []
