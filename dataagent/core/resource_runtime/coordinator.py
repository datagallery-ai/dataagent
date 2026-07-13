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
"""ResourceJobCoordinator: northbound resource job orchestration for builtin tools."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from dataagent.core.jobs.envelope import (
    SUBMIT_RESOURCE_JOB_TOOL,
    build_base_job_envelope,
    finalize_job_envelope,
)
from dataagent.core.jobs.service import JobService
from dataagent.core.resource_runtime.mcp import build_mcp_client_from_driver, build_mcp_resource_client
from dataagent.core.resource_runtime.operations.operations import ResourceOperationRegistry
from dataagent.core.resource_runtime.operations.protocols import McpResourceClient
from dataagent.core.resource_runtime.runner import build_resource_runner
from dataagent.resources.capacity.ledger import ResourceCapacity
from dataagent.resources.catalog.catalog import ResourceCatalog
from dataagent.resources.catalog.models import Resource
from dataagent.resources.resolve.prepare import DriverBinding, ResourceResolve


class ResourceJobCoordinator:
    """Coordinate resource lifecycle tools against catalog, capacity, and JobService."""

    def __init__(
        self,
        *,
        catalog: ResourceCatalog,
        capacity: ResourceCapacity,
        resolve: ResourceResolve,
        job_service: JobService,
        runtime: Any,
        operation_registry: ResourceOperationRegistry,
        mcp_client_factory: Callable[[Resource], McpResourceClient] | None = None,
    ) -> None:
        """Bind catalog, capacity, resolve, job service, runtime, and driver registry."""
        self.catalog = catalog
        self.capacity = capacity
        self.resolve = resolve
        self._job_service = job_service
        self.runtime = runtime
        self.operation_registry = operation_registry
        self._mcp_client_factory = mcp_client_factory
        self._mcp_clients: dict[str, McpResourceClient] = {}

    @property
    def job_service(self) -> JobService:
        """Return the workspace-scoped job service."""
        return self._job_service

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

        job_id = JobService.new_job_id()
        reserve = self.capacity.try_reserve(
            resource_id=resource.id,
            task_type=task_type,
            job_id=job_id,
            amount=amount,
        )
        if not reserve.ok:
            return {"status": "ERROR", "message": reserve.message}

        plan = self.resolve.prepare_submit(resource=resource, envelope=envelope, amount=amount)
        runner = build_resource_runner(
            coordinator=self,
            resource=resource,
            envelope=envelope,
            plan=plan,
        )
        resolved_timeout_sec = int(envelope.get("timeout_sec") or 3600)
        resolved_parent_tool_call_id = str(envelope.get("parent_tool_call_id") or "")
        try:
            handle = self.job_service.start(
                job_id=job_id,
                agent_id=plan.agent_id,
                task=plan.task,
                runner=runner,
                metadata=plan.metadata,
                allocation=plan.allocation,
                timeout_sec=resolved_timeout_sec,
                parent_tool_call_id=resolved_parent_tool_call_id,
            )
        except Exception as exc:
            self.capacity.release(job_id)
            message = str(exc).strip() or exc.__class__.__name__
            return {"status": "ERROR", "message": f"failed to start resource job: {message}"}

        return {
            "status": handle["status"],
            "job_id": handle["job_id"],
            "resource_id": resource.id,
            "allocation": plan.allocation["resource"],
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
        result = self.job_service.cancel(job_id).to_dict()
        return result

    def list_resources(self) -> dict[str, Any]:
        """Return a read-only catalog with runtime used/available counts."""
        usage = {view.id: view for view in self.capacity.snapshot()}
        return {
            "status": "OK",
            "resources": [
                {
                    "id": resource.id,
                    "name": resource.name,
                    "category": resource.category,
                    "capacity": {"total": resource.capacity, "unit": resource.unit},
                    "used": usage[resource.id].used if resource.id in usage else 0,
                    "available": usage[resource.id].available if resource.id in usage else resource.capacity,
                }
                for resource in self.catalog.list()
            ],
        }

    def release_capacity(self, job_id: str) -> None:
        """Release capacity reserved for one job id."""
        self.capacity.release(job_id)

    def get_mcp_client(self, resource_id: str, driver: DriverBinding) -> McpResourceClient:
        """Return a cached MCP client for one executable MCP resource."""
        cached = self._mcp_clients.get(resource_id)
        if cached is not None:
            return cached
        if driver.transport_type == "mcp":
            client = build_mcp_client_from_driver(resource_id, driver)
        else:
            resource = self.catalog.get(resource_id)
            if resource is None:
                raise ValueError(f"resource not found: {resource_id}")
            if self._mcp_client_factory is None:
                client = build_mcp_resource_client(resource)
            else:
                client = self._mcp_client_factory(resource)
        self._mcp_clients[resource_id] = client
        return client

    def _pick_resource(self, *, resource_id: str, task_type: str) -> tuple[Resource | None, int, str]:
        """Select one executable resource and resolve consumption amount."""
        normalized_id = str(resource_id or "").strip()
        if normalized_id:
            resource, error = self.catalog.select_executable(resource_id=normalized_id, task_type=task_type)
            if resource is None:
                return None, 0, error
            amount = resource.consumption_for(task_type)
            if amount is None:
                return None, 0, f"resource {normalized_id} does not declare consumption for task type: {task_type}"
            return resource, int(amount), ""

        candidates: list[tuple[Resource, int]] = []
        for resource in self.catalog.executable_resources():
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


def _local_command_error(command: str) -> str:
    """Validate that a local resource command is non-empty."""
    if not str(command or "").strip():
        return "command is required"
    return ""


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
