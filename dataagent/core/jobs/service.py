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
"""JobService: queued → running → terminal lifecycle with background runners."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock, Thread, Timer
from typing import Any

from loguru import logger

from dataagent.core.jobs.file_store import FileJobStore
from dataagent.core.jobs.models import TERMINAL_STATUSES, JobResult, JobSnapshot


@dataclass
class RunningJob:
    """In-process handle for one active background job."""

    job_id: str
    cancel_event: Event
    timed_out_event: Event


def _not_found_snapshot(job_id: str, *, cursor: str | None) -> JobSnapshot:
    # Invalid or missing job IDs should not reveal filesystem details.
    try:
        next_cursor = str(max(0, int(cursor or 0)))
    except (TypeError, ValueError):
        next_cursor = "0"
    return JobSnapshot(
        job_id=str(job_id or ""),
        agent_id="",
        status="not_found",
        cursor=next_cursor,
        events=[],
        metadata={},
        request={},
        allocation={},
    )


class JobService:
    """Manage asynchronous jobs backed by :class:`FileJobStore`."""

    def __init__(self, store: FileJobStore) -> None:
        """Create a service bound to one workspace-scoped store."""
        self.store = store
        self._running: dict[str, RunningJob] = {}
        self._lock = Lock()
        self._reconcile_orphaned_jobs()

    @staticmethod
    def new_job_id() -> str:
        """Allocate a new opaque job id."""
        return uuid.uuid4().hex

    def start(
        self,
        *,
        job_id: str | None = None,
        agent_id: str,
        task: str,
        runner: Callable[[str, Event], JobResult | dict[str, Any]],
        metadata: dict[str, Any] | None = None,
        allocation: dict[str, Any] | None = None,
        timeout_sec: int | None = None,
        parent_tool_call_id: str = "",
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Queue a job and start its background runner thread."""
        job_id = str(job_id or "").strip() or JobService.new_job_id()
        normalized_timeout = _optional_positive_timeout(timeout_sec)
        job_metadata = dict(metadata or {})
        if normalized_timeout is not None:
            job_metadata["deadline_at_ms"] = int((time.time() + normalized_timeout) * 1000)
        payload = {
            "job_id": job_id,
            "agent_id": agent_id,
            "status": "queued",
            "task": str(task or "").strip(),
            "metadata": job_metadata,
            "request": {"task": str(task or "").strip(), "timeout_sec": normalized_timeout},
            "allocation": dict(allocation or {}),
        }
        self.store.write_status(job_id, payload)
        self._record_event(
            job_id,
            {
                "type": "agent_job_start",
                "job_id": job_id,
                "agent_id": agent_id,
                "status": "queued",
                "parent_tool_call_id": parent_tool_call_id,
            },
            event_sink=event_sink,
        )
        cancel_event = Event()
        timed_out_event = Event()
        terminal_lock = Lock()
        timeout_timer: Timer | None = None
        running = RunningJob(job_id=job_id, cancel_event=cancel_event, timed_out_event=timed_out_event)

        def mark_timed_out() -> None:
            timed_out_event.set()
            cancel_event.set()
            with terminal_lock:
                status = self.store.read_status(job_id)
                if str(status.get("status") or "").strip().lower() in TERMINAL_STATUSES:
                    return
                error = "job_deadline_exceeded"
                metadata = status.get("metadata") if isinstance(status.get("metadata"), dict) else {}
                result = JobResult(
                    job_id=job_id,
                    agent_id=agent_id,
                    status="timed_out",
                    summary=f"Job timed out after {normalized_timeout} seconds.",
                    error=error,
                    subagent_session_id=str(metadata.get("subagent_session_id") or ""),
                    workspace_rel_path=str(metadata.get("workspace_rel_path") or ""),
                )
                self.store.write_result(result)
                self.store.write_status(job_id, {"status": "timed_out", "error": error})
                self._record_event(
                    job_id,
                    {
                        "type": "agent_job_end",
                        "job_id": job_id,
                        "agent_id": agent_id,
                        "status": "timed_out",
                        "reason": error,
                        "parent_tool_call_id": parent_tool_call_id,
                    },
                    event_sink=event_sink,
                )

        if normalized_timeout is not None:
            timeout_timer = Timer(normalized_timeout, mark_timed_out)
            timeout_timer.daemon = True

        def target() -> None:
            self.store.write_status(job_id, {"status": "running"})
            self._record_event(
                job_id,
                {
                    "type": "agent_job_update",
                    "job_id": job_id,
                    "agent_id": agent_id,
                    "status": "running",
                    "parent_tool_call_id": parent_tool_call_id,
                },
                event_sink=event_sink,
            )
            try:
                raw = runner(job_id, cancel_event)
                result = raw if isinstance(raw, JobResult) else _job_result_from_payload(raw, job_id, agent_id)
                with terminal_lock:
                    if timed_out_event.is_set():
                        return
                    status = self.store.read_status(job_id)
                    status_was_cancelled = str(status.get("status") or "").strip().lower() == "cancelled"
                    if cancel_event.is_set() or status_was_cancelled:
                        result.status = "cancelled"
                        if not result.summary:
                            result.summary = "Agent job cancelled."
                        result.error = ""
                    self.store.write_result(result)
                    self.store.write_status(job_id, {"status": result.status})
                    self._record_event(
                        job_id,
                        {
                            "type": "agent_job_end",
                            "job_id": job_id,
                            "agent_id": agent_id,
                            "status": result.status,
                            "parent_tool_call_id": parent_tool_call_id,
                        },
                        event_sink=event_sink,
                    )
            except Exception as exc:
                result = JobResult(
                    job_id=job_id,
                    agent_id=agent_id,
                    status="failed",
                    error=str(exc),
                    summary=f"Agent job failed: {exc}",
                )
                with terminal_lock:
                    if timed_out_event.is_set():
                        return
                    self.store.write_result(result)
                    self.store.write_status(job_id, {"status": "failed", "error": str(exc)})
                    self._record_event(
                        job_id,
                        {
                            "type": "agent_job_end",
                            "job_id": job_id,
                            "agent_id": agent_id,
                            "status": "failed",
                            "parent_tool_call_id": parent_tool_call_id,
                        },
                        event_sink=event_sink,
                    )
            finally:
                if timeout_timer is not None:
                    timeout_timer.cancel()
                with self._lock:
                    self._running.pop(job_id, None)

        thread = Thread(target=target, name=f"ferry-agent-job-{job_id[:8]}", daemon=True)
        with self._lock:
            self._running[job_id] = running
        thread.start()
        if timeout_timer is not None:
            timeout_timer.start()
        return {"job_id": job_id, "agent_id": agent_id, "status": "queued"}

    def poll(self, job_id: str, *, cursor: str | None = None, event_limit: int = 20) -> JobSnapshot:
        """Return current status and incremental events."""
        try:
            status = self.store.read_status(job_id)
            events, next_cursor = self.store.read_events(job_id, cursor=cursor, limit=event_limit)
        except ValueError:
            return _not_found_snapshot(job_id, cursor=cursor)
        return JobSnapshot(
            job_id=str(status.get("job_id") or job_id),
            agent_id=str(status.get("agent_id") or ""),
            status=str(status.get("status") or "unknown"),
            cursor=next_cursor,
            events=events,
            metadata=status.get("metadata") if isinstance(status.get("metadata"), dict) else {},
            request=_dict_status_field(status, "request"),
            allocation=_dict_status_field(status, "allocation"),
        )

    def collect(self, job_id: str) -> dict[str, Any]:
        """Read the persisted terminal result when available."""
        try:
            status = self.store.read_status(job_id)
            result = self.store.read_result(job_id)
        except ValueError:
            return {"job_id": str(job_id or ""), "agent_id": "", "status": "not_found", "message": "Job not found."}
        terminal = str(status.get("status") or "").strip().lower()
        if result is None:
            return {
                "job_id": job_id,
                "agent_id": str(status.get("agent_id") or ""),
                "status": terminal or "unknown",
                "message": "Agent job has not produced a final result yet.",
            }
        merged = dict(result)
        merged.setdefault("job_id", job_id)
        merged.setdefault("agent_id", str(status.get("agent_id") or ""))
        merged.setdefault("status", terminal or str(result.get("status") or "unknown"))
        _merge_job_metadata_fields(merged, status)
        return merged

    def cancel(self, job_id: str) -> JobSnapshot:
        """Cancel a running job and persist a terminal cancelled result."""
        with self._lock:
            running = self._running.get(job_id)
        try:
            status = self.store.read_status(job_id)
        except ValueError:
            return _not_found_snapshot(job_id, cursor=None)
        if str(status.get("status") or "") in TERMINAL_STATUSES:
            return self.poll(job_id)
        if running is not None:
            running.cancel_event.set()
        agent_id = str(status.get("agent_id") or "")
        self.store.write_status(job_id, {"status": "cancelled"})
        self.store.append_event(
            job_id,
            {"type": "agent_job_end", "job_id": job_id, "agent_id": agent_id, "status": "cancelled"},
        )
        metadata = status.get("metadata") if isinstance(status.get("metadata"), dict) else {}
        result = JobResult(
            job_id=job_id,
            agent_id=agent_id,
            status="cancelled",
            summary="Agent job cancelled.",
            subagent_session_id=str(metadata.get("subagent_session_id") or ""),
            workspace_rel_path=str(metadata.get("workspace_rel_path") or ""),
        )
        self.store.write_result(result)
        return self.poll(job_id)

    def running_job_ids(self) -> set[str]:
        """Return job ids currently tracked in-process for this service instance."""
        with self._lock:
            return set(self._running.keys())

    def _reconcile_orphaned_jobs(self) -> None:
        """Mark persisted queued/running jobs as failed when no runner is active."""
        try:
            statuses = self.store.list_statuses()
        except OSError as exc:
            logger.warning("Failed to list job statuses during orphan reconciliation: {}", exc)
            return
        with self._lock:
            running_ids = set(self._running)
        for status in statuses:
            if not isinstance(status, dict):
                continue
            job_id = str(status.get("job_id") or "").strip()
            if not job_id or job_id in running_ids:
                continue
            current_status = str(status.get("status") or "").strip().lower()
            if current_status not in {"queued", "running"}:
                continue
            agent_id = str(status.get("agent_id") or "")
            message = "Job runner is not active in this process; marking the persisted job as failed."
            result = JobResult(
                job_id=job_id,
                agent_id=agent_id,
                status="failed",
                summary=message,
                error="orphaned_job_runner",
            )
            self.store.write_result(result)
            self.store.write_status(job_id, {"status": "failed", "error": result.error})
            self.store.append_event(
                job_id,
                {
                    "type": "agent_job_end",
                    "job_id": job_id,
                    "agent_id": agent_id,
                    "status": "failed",
                    "reason": result.error,
                },
            )

    def _record_event(
        self,
        job_id: str,
        event: dict[str, Any],
        *,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Append one event and optionally forward it to a sink."""
        self.store.append_event(job_id, event)
        if callable(event_sink):
            event_sink(event)


def _dict_status_field(status: dict[str, Any], key: str) -> dict[str, Any]:
    value = status.get(key)
    return value if isinstance(value, dict) else {}


def _optional_positive_timeout(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _job_result_from_payload(payload: dict[str, Any], job_id: str, agent_id: str) -> JobResult:
    safe = payload if isinstance(payload, dict) else {}
    return JobResult(
        job_id=str(safe.get("job_id") or job_id),
        agent_id=str(safe.get("agent_id") or agent_id),
        status=str(safe.get("status") or "completed"),
        summary=str(safe.get("summary") or safe.get("frontend_msg") or ""),
        error=str(safe.get("error") or ""),
        original_msg=safe.get("original_msg"),
        frontend_msg=str(safe.get("frontend_msg") or ""),
        state=safe.get("state") if isinstance(safe.get("state"), dict) else None,
        subagent_session_id=str(safe.get("subagent_session_id") or ""),
        workspace_rel_path=str(safe.get("workspace_rel_path") or ""),
        outputs=safe.get("outputs") if isinstance(safe.get("outputs"), list) else [],
        metrics=safe.get("metrics") if isinstance(safe.get("metrics"), dict) else {},
    )


def _merge_job_metadata_fields(collected: dict[str, Any], status: dict[str, Any]) -> None:
    """Copy workspace identifiers from ``job.json`` metadata when missing in ``result.json``."""
    metadata = status.get("metadata")
    if not isinstance(metadata, dict):
        return
    if not str(collected.get("subagent_session_id") or "").strip():
        session_id = str(metadata.get("subagent_session_id") or "").strip()
        if session_id:
            collected["subagent_session_id"] = session_id
    if not str(collected.get("workspace_rel_path") or "").strip():
        workspace_rel_path = str(metadata.get("workspace_rel_path") or "").strip()
        if workspace_rel_path:
            collected["workspace_rel_path"] = workspace_rel_path
