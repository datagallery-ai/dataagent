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
"""AgentService: submit / poll / collect / cancel for subagent jobs."""

from __future__ import annotations

from threading import Event
from typing import Any

from loguru import logger

from dataagent.core.agents.adapters.local_flex import LocalFlexAdapter
from dataagent.core.agents.registry import AgentRegistry, AgentResolution, AgentSpec
from dataagent.core.agents.subagent_session import SubagentWorkspaceSession, resolve_subagent_workspace_session
from dataagent.core.jobs.envelope import (
    _PROTECTED_FIELDS,
    SUBMIT_SUBAGENT_TOOL,
    build_base_job_envelope,
    finalize_job_envelope,
)
from dataagent.core.jobs.models import JobResult, agent_binding
from dataagent.core.jobs.service import JobService
from dataagent.utils.constants import DEFAULT_JOBS_SUBAGENTS_MAX, DEFAULT_SUBMIT_SUBAGENT_TIMEOUT_SEC

AGENT_JOB_TOOL_NAMES = frozenset({"submit_subagent", "poll_subagent", "collect_subagent", "cancel_subagent"})

_ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})


class AgentService:
    """Northbound subagent job API used by lifecycle tools."""

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        job_service: JobService,
        runtime: Any,
        adapter: LocalFlexAdapter | None = None,
    ) -> None:
        """Bind registry, job service, runtime, and optional adapter."""
        self.registry = registry
        self.job_service = job_service
        self.runtime = runtime
        self._adapter = adapter or LocalFlexAdapter()

    def submit(
        self,
        *,
        agent_id: str,
        task: str,
        timeout_sec: int = DEFAULT_SUBMIT_SUBAGENT_TIMEOUT_SEC,
        parent_tool_call_id: str = "",
        job_envelope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Queue one subagent job in a new or reused workspace."""
        parent_ws = getattr(self.runtime, "workspace_dir", None)
        if parent_ws is None:
            return {"status": "ERROR", "message": "submit_subagent requires a resolved parent workspace."}

        envelope, error = _prepare_subagent_submit_envelope(
            agent_id=agent_id,
            task=task,
            timeout_sec=timeout_sec,
            parent_tool_call_id=parent_tool_call_id,
            job_envelope=job_envelope,
        )
        if error is not None:
            return error

        spec, resolution, error = _validate_submit_agent_and_capacity(
            self.registry,
            self.runtime,
            self.job_service,
            envelope,
        )
        if error is not None:
            return error
        if spec is None or resolution is None:
            return {"status": "ERROR", "message": "agent resolution failed"}

        session, error = _resolve_submit_workspace_session(self.runtime, parent_ws, envelope)
        if error is not None:
            return error
        if session is None:
            return {"status": "ERROR", "message": "workspace session resolution failed"}

        return self._enqueue_subagent_job(
            spec=spec,
            resolution=resolution,
            envelope=envelope,
            session=session,
        )

    def poll(self, *, job_id: str, cursor: str | None = None, event_limit: int = 20) -> dict[str, Any]:
        """Poll one job and return a JSON-serializable snapshot."""
        snapshot = self.job_service.poll(job_id, cursor=cursor, event_limit=event_limit)
        payload = snapshot.to_dict()
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        self._maybe_refresh_workspace_catalog_artifacts(
            status=str(payload.get("status") or ""),
            subagent_session_id=str(metadata.get("subagent_session_id") or ""),
        )
        return payload

    def collect(self, *, job_id: str) -> dict[str, Any]:
        """Collect the terminal result for one job."""
        result = self.job_service.collect(job_id)
        self._update_workspace_catalog_on_collect(result)
        return result

    def _update_workspace_catalog_on_submit(
        self,
        *,
        session: SubagentWorkspaceSession,
        envelope: dict[str, Any],
        handle: dict[str, Any],
        reused: bool,
    ) -> None:
        parent_ws = getattr(self.runtime, "workspace_dir", None)
        if parent_ws is None:
            return
        from dataagent.core.workspace.catalog import safe_append_job, safe_register_environment

        subagent_id = session.subagent_session_id
        if not reused:
            safe_register_environment(parent_ws, subagent_id)
        safe_append_job(
            parent_ws,
            subagent_id,
            job_id=str(handle.get("job_id") or ""),
            agent_id=str(handle.get("agent_id") or ""),
            task=str(envelope.get("task") or ""),
        )

    def cancel(self, *, job_id: str) -> dict[str, Any]:
        """Cancel one running job."""
        return self.job_service.cancel(job_id).to_dict()

    def _maybe_refresh_workspace_catalog_artifacts(
        self,
        *,
        status: str,
        subagent_session_id: str,
    ) -> None:
        """Refresh catalog ``artifacts`` when a job reaches ``completed``."""
        if str(status or "").strip().lower() != "completed":
            return
        parent_ws = getattr(self.runtime, "workspace_dir", None)
        subagent_id = str(subagent_session_id or "").strip()
        if parent_ws is None or not subagent_id:
            return
        from dataagent.core.workspace.catalog import safe_refresh_artifacts

        safe_refresh_artifacts(parent_ws, subagent_id, config=_runtime_config(self.runtime))

    def _update_workspace_catalog_on_collect(self, result: dict[str, Any]) -> None:
        self._maybe_refresh_workspace_catalog_artifacts(
            status=str(result.get("status") or ""),
            subagent_session_id=str(result.get("subagent_session_id") or ""),
        )

    def _enqueue_subagent_job(
        self,
        *,
        spec: AgentSpec,
        resolution: AgentResolution,
        envelope: dict[str, Any],
        session: SubagentWorkspaceSession,
    ) -> dict[str, Any]:
        """Start the background job and return the queued submit payload."""
        resolved_workspace_rel_path = str(envelope.get("workspace_rel_path") or "").strip()
        normalized_task = str(envelope.get("task") or "").strip()
        normalized_agent_id = spec.id
        envelope["agent_id"] = normalized_agent_id
        resolved_timeout_sec = int(envelope.get("timeout_sec") or DEFAULT_SUBMIT_SUBAGENT_TIMEOUT_SEC)
        resolved_parent_tool_call_id = str(envelope.get("parent_tool_call_id") or "")
        reused = bool(resolved_workspace_rel_path)
        job_id = JobService.new_job_id()

        def emit(event: dict[str, Any]) -> None:
            event_job_id = str(event.get("job_id") or "")
            if event_job_id:
                self.job_service.store.append_event(event_job_id, event)

        def runner(job_id: str, cancel_event: Event) -> JobResult:
            return self._adapter.run(
                job_id=job_id,
                spec=spec,
                task=normalized_task,
                workspace_dir=session.workspace_dir,
                subagent_session_id=session.subagent_session_id,
                workspace_rel_path=session.workspace_rel_path,
                runtime=self.runtime,
                cancel_event=cancel_event,
                emit_event=emit,
                parent_tool_call_id=resolved_parent_tool_call_id,
                reuse_workspace=reused,
                timeout_sec=resolved_timeout_sec,
            )

        metadata = {
            "subagent_session_id": session.subagent_session_id,
            "workspace_rel_path": session.workspace_rel_path,
            "parent_session_id": str(getattr(self.runtime, "session_id", "") or ""),
            "job_envelope": dict(envelope),
            "reused_workspace": reused,
        }
        handle = self.job_service.start(
            job_id=job_id,
            agent_id=normalized_agent_id,
            task=normalized_task,
            runner=runner,
            allocation=agent_binding(pool="local"),
            timeout_sec=resolved_timeout_sec,
            parent_tool_call_id=resolved_parent_tool_call_id,
            metadata=metadata,
        )
        self._update_workspace_catalog_on_submit(
            session=session,
            envelope=envelope,
            handle=handle,
            reused=reused,
        )
        message = (
            "Agent job queued on reused workspace. Use poll_subagent or collect_subagent with this job_id."
            if reused
            else "Agent job queued. Use poll_subagent or collect_subagent with this job_id."
        )
        return {
            "status": handle["status"],
            "agent_id": handle["agent_id"],
            "job_id": handle["job_id"],
            "subagent_session_id": session.subagent_session_id,
            "workspace_rel_path": session.workspace_rel_path,
            "agent_id_matched_by": resolution.matched_by,
            "reused_workspace": reused,
            "message": message,
        }


def _prepare_subagent_submit_envelope(
    *,
    agent_id: str,
    task: str,
    timeout_sec: int,
    parent_tool_call_id: str,
    job_envelope: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Build and finalize the submit envelope.

    Returns:
        ``(envelope, error_payload)``; exactly one entry is non-``None``.
    """
    merged_envelope = dict(job_envelope or {})
    tool_args: dict[str, Any] = {
        "agent_id": agent_id,
        "task": task,
        "timeout_sec": timeout_sec,
    }
    workspace_rel_path = str(merged_envelope.get("workspace_rel_path") or "").strip()
    if workspace_rel_path:
        tool_args["workspace_rel_path"] = workspace_rel_path

    base_envelope = build_base_job_envelope(
        SUBMIT_SUBAGENT_TOOL,
        tool_args,
        parent_tool_call_id=str(merged_envelope.get("parent_tool_call_id") or parent_tool_call_id),
    )
    if base_envelope is None:
        return None, {"status": "ERROR", "message": "submit_subagent envelope build failed"}
    candidate = _merge_job_envelope_candidate(SUBMIT_SUBAGENT_TOOL, base_envelope, merged_envelope)
    try:
        envelope = finalize_job_envelope(SUBMIT_SUBAGENT_TOOL, base_envelope, candidate)
    except ValueError as exc:
        return None, {"status": "ERROR", "message": str(exc)}
    if not str(envelope.get("task") or "").strip():
        return None, {"status": "ERROR", "message": "task is required"}
    return envelope, None


def _validate_submit_agent_and_capacity(
    registry: AgentRegistry,
    runtime: Any,
    job_service: JobService,
    envelope: dict[str, Any],
) -> tuple[AgentSpec | None, AgentResolution | None, dict[str, Any] | None]:
    """Resolve agent spec and enforce concurrency / workspace-busy constraints."""
    resolution = registry.resolve(str(envelope.get("agent_id") or "").strip())
    spec = resolution.spec
    if spec is None:
        return (
            None,
            None,
            {
                "status": "ERROR",
                "message": f"agent not found: {resolution.agent_id}",
                "available_agent_ids": [item.id for item in registry.list()],
                "suggestions": list(resolution.suggestions),
            },
        )

    limit = _subagent_limit(runtime)
    active = _active_subagent_count(job_service)
    if active >= limit:
        return (
            None,
            None,
            {
                "status": "ERROR",
                "message": f"subagent capacity exhausted: {active}/{limit} running",
                "resource": {"pool": "subagent_concurrency", "used": active, "capacity": limit},
            },
        )

    resolved_workspace_rel_path = str(envelope.get("workspace_rel_path") or "").strip()
    if resolved_workspace_rel_path:
        busy_job_id = _active_job_id_for_workspace(
            job_service,
            workspace_rel_path=resolved_workspace_rel_path,
        )
        if busy_job_id:
            return (
                None,
                None,
                {
                    "status": "ERROR",
                    "message": (
                        f"workspace_rel_path is busy: {resolved_workspace_rel_path} (active job_id={busy_job_id})"
                    ),
                },
            )
    return spec, resolution, None


def _resolve_submit_workspace_session(
    runtime: Any,
    parent_ws: Any,
    envelope: dict[str, Any],
) -> tuple[SubagentWorkspaceSession | None, dict[str, Any] | None]:
    """Resolve or allocate the subagent workspace for one submit."""
    resolved_workspace_rel_path = str(envelope.get("workspace_rel_path") or "").strip()
    try:
        session = resolve_subagent_workspace_session(
            parent_workspace=parent_ws,
            workspace_rel_path=resolved_workspace_rel_path or None,
            config=_runtime_config(runtime),
        )
    except ValueError as exc:
        return None, {"status": "ERROR", "message": str(exc)}
    return session, None


def _merge_job_envelope_candidate(
    tool_name: str,
    base_envelope: dict[str, Any],
    merged_envelope: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge plugin-supplied envelope fields onto the core-built baseline.

    Core-owned protected fields always come from ``base_envelope``; callers may
    supply only supplemental keys (for example reuse ``workspace_rel_path`` via
    ``tool_args`` extraction, or plugin metadata such as ``run_id``).
    """
    candidate = dict(base_envelope)
    if not merged_envelope:
        return candidate
    protected = _PROTECTED_FIELDS.get(tool_name, frozenset())
    for key, value in merged_envelope.items():
        if key in protected:
            continue
        candidate[key] = value
    return candidate


def _runtime_config(runtime: Any) -> dict[str, Any] | None:
    """Return merged agent config from ``runtime`` when available."""
    get_all_config = getattr(runtime, "get_all_config", None)
    if callable(get_all_config):
        config = get_all_config()
        if isinstance(config, dict):
            return config
    return None


def _subagent_limit(runtime: Any) -> int:
    """Read ``JOBS.subagents.max`` from the runtime config manager."""
    cm = getattr(getattr(runtime, "env", None), "config_manager", None)
    if cm is not None:
        try:
            raw = cm.get("JOBS.subagents.max", DEFAULT_JOBS_SUBAGENTS_MAX)
            return max(1, int(raw))
        except (TypeError, ValueError) as exc:
            logger.debug("Invalid JOBS.subagents.max config value; using default: {}", exc)
    return DEFAULT_JOBS_SUBAGENTS_MAX


def _list_job_statuses(job_service: JobService) -> list[dict[str, Any]]:
    """List persisted job statuses, returning an empty list when the store is unreadable."""
    try:
        return job_service.store.list_statuses()
    except OSError as exc:
        logger.warning("Failed to list job statuses: {}", exc)
        return []


def _active_subagent_count(job_service: JobService) -> int:
    """Count queued/running jobs from the on-disk store."""
    statuses = _list_job_statuses(job_service)
    active_from_store = {
        str(item.get("job_id") or "")
        for item in statuses
        if str(item.get("status") or "").strip().lower() in _ACTIVE_JOB_STATUSES
    }
    return len({job_id for job_id in active_from_store if job_id})


def _active_job_id_for_workspace(job_service: JobService, *, workspace_rel_path: str) -> str | None:
    """Return an active job id already bound to ``workspace_rel_path``, if any."""
    target = str(workspace_rel_path or "").strip()
    if not target:
        return None
    statuses = _list_job_statuses(job_service)
    for item in statuses:
        if str(item.get("status") or "").strip().lower() not in _ACTIVE_JOB_STATUSES:
            continue
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if str(metadata.get("workspace_rel_path") or "").strip() == target:
            job_id = str(item.get("job_id") or "").strip()
            if job_id:
                return job_id
    return None
