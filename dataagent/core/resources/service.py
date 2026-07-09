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
"""ResourceService: submit/poll/collect/cancel for resource-backed jobs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock
from typing import Any

from loguru import logger

from dataagent.core.jobs.envelope import (
    SUBMIT_RESOURCE_JOB_TOOL,
    build_base_job_envelope,
    finalize_job_envelope,
)
from dataagent.core.jobs.models import TERMINAL_STATUSES, JobResult, resource_binding
from dataagent.core.jobs.service import JobService
from dataagent.core.resources.models import Resource
from dataagent.core.resources.operations import (
    ResourceOperationContext,
    ResourceOperationRegistry,
)
from dataagent.core.resources.protocols import McpResourceClient
from dataagent.core.resources.registry import ResourceRegistry

_REMOTE_TERMINAL_STATUSES = frozenset({"completed", "success", "succeeded", "failed", "error", "cancelled", "canceled"})


class ResourceService:
    """Northbound resource job API used by lifecycle tools."""

    def __init__(
        self,
        *,
        registry: ResourceRegistry,
        job_service: JobService,
        runtime: Any,
        operation_registry: ResourceOperationRegistry,
        mcp_client_factory: Callable[[Resource], McpResourceClient] | None = None,
    ) -> None:
        """Bind registry, job service, runtime, and injected driver implementations."""
        self.registry = registry
        self.job_service = job_service
        self.runtime = runtime
        self.operation_registry = operation_registry
        self._mcp_client_factory = mcp_client_factory
        self._allocation_lock = Lock()
        self._pending_usage: dict[str, int] = {}
        self._mcp_clients: dict[str, McpResourceClient] = {}

    def submit_job(
        self,
        *,
        resource_id: str = "",
        command: str = "",
        script_artifact: dict[str, Any] | None = None,
        inputs: list[dict[str, Any]] | None = None,
        outputs: list[dict[str, Any]] | None = None,
        task_type: str = "",
        receipt_ids: list[str] | None = None,
        out_kind: str = "",
        job_envelope: dict[str, Any] | None = None,
        timeout_sec: int = 3600,
        sandbox_request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Queue one resource job after capacity validation and envelope merge."""
        parent_ws = getattr(self.runtime, "workspace_dir", None)
        if parent_ws is None:
            return {"status": "ERROR", "message": "submit_resource_job requires a resolved parent workspace."}

        base_envelope = build_base_job_envelope(
            SUBMIT_RESOURCE_JOB_TOOL,
            {
                "command": command,
                "resource_id": resource_id,
                "script_artifact": script_artifact,
                "inputs": inputs,
                "outputs": outputs,
                "task_type": task_type,
                "receipt_ids": receipt_ids,
                "out_kind": out_kind,
                "timeout_sec": timeout_sec,
                "sandbox_request": sandbox_request,
            },
            parent_tool_call_id=str((job_envelope or {}).get("parent_tool_call_id") or ""),
        )
        if base_envelope is None:
            return {"status": "ERROR", "message": "submit_resource_job envelope build failed"}
        try:
            envelope = finalize_job_envelope(
                SUBMIT_RESOURCE_JOB_TOOL,
                base_envelope,
                job_envelope or base_envelope,
            )
        except ValueError as exc:
            return {"status": "ERROR", "message": str(exc)}

        task_type = str(envelope["type"])
        resource, amount, error = self._pick_resource(
            resource_id=str(envelope.get("resource_id") or ""),
            task_type=task_type,
        )
        if resource is None:
            return {"status": "ERROR", "message": error}
        envelope["resource_id"] = resource.id

        transport_type = str((resource.transport or {}).get("type") or "").strip().lower()
        if transport_type not in {"local", "mcp"}:
            return {"status": "ERROR", "message": f"unsupported resource transport: {transport_type or '<empty>'}"}

        normalized_command = str(envelope.get("command") or "").strip()
        if transport_type == "local" and resource.operations.get("submit") == "sandbox.submit":
            envelope.setdefault(
                "sandbox_request",
                {"enabled": True, "backend": "best-effort"},
            )
            command_error = _local_command_error(normalized_command)
            if command_error:
                return {"status": "ERROR", "message": command_error}
        elif transport_type == "mcp":
            if not normalized_command:
                return {"status": "ERROR", "message": "command or job_envelope is required"}
        elif not normalized_command:
            return {"status": "ERROR", "message": "command or job_envelope is required"}

        effective_script_artifact = envelope.get("script_artifact")
        if isinstance(effective_script_artifact, dict):
            script_path = _script_path_from_artifact(effective_script_artifact, Path(parent_ws))
            if script_path is None:
                return {"status": "ERROR", "message": "script_artifact must point to a file inside the workspace"}
            envelope["script_artifact"] = {**effective_script_artifact, "path": str(script_path)}

        allocation = resource_binding(
            resource.id,
            task_type=task_type,
            amount=amount,
            unit=resource.unit,
        )
        resolved_timeout_sec = int(envelope.get("timeout_sec") or 3600)
        resolved_parent_tool_call_id = str(envelope.get("parent_tool_call_id") or "")
        normalized_task = normalized_command or task_type

        def runner(job_id: str, cancel_event: Event) -> JobResult:
            if cancel_event.is_set():
                return JobResult(
                    job_id=job_id,
                    agent_id=f"resource:{resource.id}",
                    status="cancelled",
                    summary="Resource job cancelled.",
                )
            result = self._run_resource(
                resource=resource,
                envelope=envelope,
                allocation=allocation["resource"],
                cancel_event=cancel_event,
            )
            status = _job_status(result, cancelled=cancel_event.is_set())
            exit_code = int(result.get("exit_code", 0 if status == "completed" else 1))
            return JobResult(
                job_id=job_id,
                agent_id=f"resource:{resource.id}",
                status=status,
                summary=_summary_text(result)[:2000],
                outputs=_outputs(result),
                metrics={
                    "exit_code": exit_code,
                    **(result.get("metrics") if isinstance(result.get("metrics"), dict) else {}),
                },
                error=_error_text(result) if status == "failed" else "",
            )

        handle, start_error = self._start_with_capacity_check(
            resource=resource,
            amount=amount,
            agent_id=f"resource:{resource.id}",
            task=normalized_task,
            runner=runner,
            metadata={
                "job_kind": "resource",
                "resource_id": resource.id,
                "resource_category": resource.category,
                "job_envelope": dict(envelope),
            },
            allocation=allocation,
            timeout_sec=resolved_timeout_sec,
            parent_tool_call_id=resolved_parent_tool_call_id,
        )
        if handle is None:
            return {"status": "ERROR", "message": start_error}
        return {
            "status": handle["status"],
            "job_id": handle["job_id"],
            "resource_id": resource.id,
            "allocation": allocation["resource"],
            "message": "Resource job queued. Use poll_job or collect_job with this job_id.",
        }

    def poll(self, *, job_id: str, cursor: str | None = None, event_limit: int = 20) -> dict[str, Any]:
        """Poll one resource job and return a JSON-serializable snapshot."""
        return self.job_service.poll(job_id, cursor=cursor, event_limit=event_limit).to_dict()

    def collect(self, *, job_id: str) -> dict[str, Any]:
        """Collect the terminal result for one resource job."""
        return self.job_service.collect(job_id)

    def cancel(self, *, job_id: str) -> dict[str, Any]:
        """Cancel one running resource job."""
        return self.job_service.cancel(job_id).to_dict()

    def list_resources(self) -> dict[str, Any]:
        """Return a read-only catalog with runtime used/available counts."""
        usage = self._current_resource_usage()
        catalog = self.registry.with_usage(resource_usage=usage)
        return {
            "status": "OK",
            "resources": [
                {
                    "id": resource.id,
                    "name": resource.name,
                    "category": resource.category,
                    "capacity": {"total": resource.capacity, "unit": resource.unit},
                    "used": resource.used,
                    "available": resource.available,
                }
                for resource in catalog.resources()
            ],
        }

    def _current_resource_usage(self) -> dict[str, int]:
        """Aggregate active and pending allocation usage per resource id."""
        with self._allocation_lock:
            usage = _active_resource_usage(self.job_service)
            for pending_id, pending_amount in self._pending_usage.items():
                usage[pending_id] = usage.get(pending_id, 0) + int(pending_amount)
            return dict(usage)

    def _pick_resource(self, *, resource_id: str, task_type: str) -> tuple[Resource | None, int, str]:
        """Select one executable resource without reserving capacity.

        Args:
            resource_id: Optional explicit resource id.
            task_type: Task type used to resolve ``consumption``.

        Returns:
            ``(resource, amount, error_message)`` tuple.
        """
        normalized_id = str(resource_id or "").strip()
        if normalized_id:
            resource, error = self.registry.select_executable(resource_id=normalized_id, task_type=task_type)
            if resource is None:
                return None, 0, error
            amount = resource.consumption_for(task_type)
            if amount is None:
                return None, 0, f"resource {normalized_id} does not declare consumption for task type: {task_type}"
            return resource, int(amount), ""

        candidates: list[tuple[Resource, int]] = []
        for resource in self.registry.executable_resources():
            amount = resource.consumption_for(task_type)
            if amount is None:
                continue
            candidates.append((resource, int(amount)))
        if not candidates:
            return None, 0, f"no executable resource supports task type: {task_type}"
        if len(candidates) > 1:
            ids = ", ".join(item.id for item, _ in candidates)
            return None, 0, f"multiple resources support task type {task_type}; specify resource_id: {ids}"
        resource, amount = candidates[0]
        return resource, amount, ""

    def _start_with_capacity_check(
        self,
        *,
        resource: Resource,
        amount: int,
        agent_id: str,
        task: str,
        runner: Any,
        metadata: dict[str, Any],
        allocation: dict[str, Any],
        timeout_sec: int,
        parent_tool_call_id: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """Start one resource job while holding the allocation lock.

        Args:
            resource: Selected executable resource.
            amount: Slot amount required by the task.
            agent_id: Job agent id persisted with the job record.
            task: Task label stored on the job.
            runner: Background runner callable.
            metadata: Job metadata persisted with the job record.
            allocation: Allocation block persisted with the job record.
            timeout_sec: Job timeout in seconds.
            parent_tool_call_id: Parent tool call id for lineage.

        Returns:
            ``(handle, error_message)`` where handle is ``None`` when capacity is exhausted
            or ``JobService.start`` raises.
        """
        with self._allocation_lock:
            self._pending_usage[resource.id] = self._pending_usage.get(resource.id, 0) + int(amount)
            try:
                usage = _active_resource_usage(self.job_service)
                for pending_id, pending_amount in self._pending_usage.items():
                    usage[pending_id] = usage.get(pending_id, 0) + int(pending_amount)
                used = int(usage.get(resource.id, 0))
                if used > resource.capacity:
                    return (
                        None,
                        f"resource capacity exhausted: {used}/{resource.capacity} {resource.unit} allocated; "
                        f"task requires {amount}",
                    )
                try:
                    handle = self.job_service.start(
                        agent_id=agent_id,
                        task=task,
                        runner=runner,
                        metadata=metadata,
                        allocation=allocation,
                        timeout_sec=timeout_sec,
                        parent_tool_call_id=parent_tool_call_id,
                    )
                except Exception as exc:
                    message = str(exc).strip() or exc.__class__.__name__
                    return None, f"failed to start resource job: {message}"
                return handle, ""
            finally:
                self._release_pending_locked(resource.id, amount)

    def _release_pending_locked(self, resource_id: str, amount: int) -> None:
        """Release pending capacity while ``_allocation_lock`` is already held."""
        remaining = self._pending_usage.get(resource_id, 0) - int(amount)
        if remaining > 0:
            self._pending_usage[resource_id] = remaining
        else:
            self._pending_usage.pop(resource_id, None)

    def _run_resource(
        self,
        *,
        resource: Resource,
        envelope: dict[str, Any],
        allocation: dict[str, Any],
        cancel_event: Event,
    ) -> dict[str, Any]:
        """Execute submit/poll/collect operations for one resource job."""
        context = ResourceOperationContext(runtime=self.runtime, resource=resource, cancel_event=cancel_event)
        submit_result = self._invoke_operation(
            resource,
            "submit",
            {"envelope": envelope, "allocation": allocation},
            context,
        )
        if not isinstance(submit_result, dict):
            submit_result = {"status": "completed", "result": submit_result}
        remote_job_id = _remote_job_id(submit_result)
        status = str(submit_result.get("status") or "").strip().lower()
        final_result = submit_result

        if remote_job_id and status not in _REMOTE_TERMINAL_STATUSES:
            import time

            deadline = time.monotonic() + max(1, int(envelope.get("timeout_sec") or 3600))
            cursor: str | None = None
            while time.monotonic() < deadline:
                if cancel_event.is_set():
                    self._cancel_operation(resource, remote_job_id, context)
                    return {"status": "cancelled", "exit_code": 130, "error": "cancelled"}
                poll_result = self._invoke_operation(
                    resource,
                    "poll",
                    {"job_id": remote_job_id, "cursor": cursor, "event_limit": 20},
                    context,
                )
                final_result = poll_result if isinstance(poll_result, dict) else {"result": poll_result}
                status = str(final_result.get("status") or "").strip().lower()
                cursor = str(final_result.get("cursor") or cursor or "") or None
                if status in _REMOTE_TERMINAL_STATUSES:
                    break
                time.sleep(3.0)
            else:
                self._cancel_operation(resource, remote_job_id, context)
                return {"status": "failed", "exit_code": 1, "error": "resource job timed out"}

        if remote_job_id and status in _REMOTE_TERMINAL_STATUSES:
            collected = self._invoke_operation(
                resource,
                "collect",
                {"job_id": remote_job_id},
                context,
            )
            if isinstance(collected, dict):
                final_result = _merge_remote_terminal_result(final_result, collected)
        return final_result

    def _invoke_operation(
        self,
        resource: Resource,
        operation: str,
        arguments: dict[str, Any],
        context: ResourceOperationContext,
    ) -> Any:
        """Invoke one configured operation for a resource."""
        operation_id = str(resource.operations.get(operation) or "").strip()
        transport_type = str((resource.transport or {}).get("type") or "").strip().lower()
        if transport_type == "mcp":
            client = self._get_mcp_client(resource)
            return client.call_tool_sync(operation_id, arguments)
        return self.operation_registry.invoke(operation_id, arguments, context)

    def _get_mcp_client(self, resource: Resource) -> McpResourceClient:
        """Return a cached MCP client for one executable MCP resource."""
        cached = self._mcp_clients.get(resource.id)
        if cached is not None:
            return cached
        if self._mcp_client_factory is None:
            raise ValueError(f"MCP client factory is not configured for resource {resource.id}")
        client = self._mcp_client_factory(resource)
        self._mcp_clients[resource.id] = client
        return client

    def _cancel_operation(
        self,
        resource: Resource,
        job_id: str,
        context: ResourceOperationContext,
    ) -> None:
        """Best-effort cancel hook for remote resource backends."""
        try:
            self._invoke_operation(resource, "cancel", {"job_id": job_id}, context)
        except Exception as exc:
            logger.warning("Best-effort resource cancel failed for job {}: {}", job_id, exc)


def _local_command_error(command: str) -> str:
    """Validate that a local resource command is non-empty."""
    if not str(command or "").strip():
        return "command is required"
    return ""


def _active_resource_usage(job_service: JobService) -> dict[str, int]:
    """Sum active allocation amounts per resource id from persisted jobs."""
    try:
        statuses = job_service.store.list_statuses()
    except OSError as exc:
        logger.warning("Failed to list job statuses for resource usage: {}", exc)
        return {}
    usage: dict[str, int] = {}
    for status in statuses:
        if not isinstance(status, dict) or str(status.get("status") or "").strip().lower() in TERMINAL_STATUSES:
            continue
        allocation = status.get("allocation") if isinstance(status.get("allocation"), dict) else {}
        resource = allocation.get("resource") if isinstance(allocation.get("resource"), dict) else {}
        resource_id = str(resource.get("id") or "").strip()
        if resource_id:
            usage[resource_id] = usage.get(resource_id, 0) + max(0, int(resource.get("amount") or 0))
    return usage


def _remote_job_id(payload: Any) -> str:
    """Extract a remote backend job id from an operation result."""
    if not isinstance(payload, dict):
        return ""
    for key in ("job_id", "remote_job_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _merge_remote_terminal_result(
    poll_result: dict[str, Any],
    collect_result: dict[str, Any],
) -> dict[str, Any]:
    """Merge poll/submit and collect payloads, preferring collect error details.

    Args:
        poll_result: Latest poll or submit payload from the remote backend.
        collect_result: Terminal collect payload from the remote backend.

    Returns:
        Combined terminal payload for resource job persistence.
    """
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


def _job_status(payload: dict[str, Any], *, cancelled: bool) -> str:
    """Map an operation result payload to a terminal job status."""
    if cancelled:
        return "cancelled"
    status = str(payload.get("status") or "").strip().lower()
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if status in {"failed", "error"} or int(payload.get("exit_code", 0) or 0) != 0:
        return "failed"
    return "completed"


def _summary_text(payload: Any) -> str:
    """Extract a human-readable summary from an operation result."""
    if isinstance(payload, dict):
        for key in ("summary", "message", "stdout", "result"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return str(payload)
    return str(payload or "")


def _error_text(payload: dict[str, Any]) -> str:
    """Extract an error string from an operation result."""
    return str(payload.get("error") or payload.get("stderr") or _summary_text(payload))


def _outputs(payload: Any) -> list[dict[str, Any]]:
    """Normalize structured outputs from an operation result."""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("outputs")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _script_path_from_artifact(script_artifact: dict[str, Any], workspace_dir: Path) -> Path | None:
    """Resolve a script artifact path inside the workspace."""
    raw_path = str(script_artifact.get("path") or "").strip()
    if not raw_path:
        return None
    workspace = workspace_dir.expanduser().resolve()
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(workspace)
    except ValueError:
        return None
    return resolved if resolved.is_file() else None
