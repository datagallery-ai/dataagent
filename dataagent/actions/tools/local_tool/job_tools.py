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
"""LLM-visible job lifecycle tools (subagent and resource tracks)."""

from __future__ import annotations

__all__ = [
    "cancel_job",
    "cancel_subagent",
    "collect_job",
    "collect_subagent",
    "list_resources",
    "poll_job",
    "poll_subagent",
    "submit_resource_job",
    "submit_subagent",
]

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.jobs.envelope import (
    DEFAULT_RESOURCE_JOB_TIMEOUT_SEC,
    envelope_from_tool_context,
)
from dataagent.core.jobs.models import TERMINAL_STATUSES
from dataagent.core.jobs.service import JobService
from dataagent.utils.constants import (
    DEFAULT_SUBMIT_SUBAGENT_TIMEOUT_SEC,
    POLL_WATCH_DEFAULT_EVENT_LIMIT,
    POLL_WATCH_DEFAULT_INTERVAL_SEC,
    POLL_WATCH_MAX_EVENT_LIMIT,
    POLL_WATCH_MAX_INTERVAL_SEC,
    POLL_WATCH_MAX_WATCH_SEC,
    POLL_WATCH_MIN_INTERVAL_SEC,
)

_RESOURCE_JOB_KIND = "resource"
_SUBAGENT_JOB_KIND = "subagent"


def _job_kind_from_status(status: dict[str, Any]) -> str:
    """Infer whether one persisted job belongs to the resource or subagent track.

    Args:
        status: Raw job status payload from :class:`~dataagent.core.jobs.service.JobService`.

    Returns:
        ``"resource"`` or ``"subagent"``.
    """
    metadata = status.get("metadata") if isinstance(status.get("metadata"), dict) else {}
    explicit = str(metadata.get("job_kind") or "").strip().lower()
    if explicit in {_RESOURCE_JOB_KIND, _SUBAGENT_JOB_KIND}:
        return explicit
    allocation = status.get("allocation") if isinstance(status.get("allocation"), dict) else {}
    if isinstance(allocation.get("resource"), dict):
        return _RESOURCE_JOB_KIND
    if str(status.get("agent_id") or "").startswith("resource:"):
        return _RESOURCE_JOB_KIND
    return _SUBAGENT_JOB_KIND


def _resolve_job_service(runtime: Any) -> JobService | None:
    """Return the workspace-scoped :class:`~dataagent.core.jobs.service.JobService`."""
    agent_service = runtime.ensure_job_services()
    if agent_service is not None and hasattr(agent_service, "job_service"):
        return agent_service.job_service
    ensure_resource_coordinator = getattr(runtime, "ensure_resource_coordinator", None)
    if callable(ensure_resource_coordinator):
        coordinator = ensure_resource_coordinator()
        if coordinator is not None and hasattr(coordinator, "job_service"):
            return coordinator.job_service
    return None


def _read_job_kind(runtime: Any, job_id: str) -> tuple[str | None, dict[str, Any] | None]:
    """Load one job status and return its inferred lifecycle track.

    Args:
        runtime: Active runtime exposing job services.
        job_id: Opaque job id from a submit tool.

    Returns:
        A ``(job_kind, error_payload)`` pair. When ``error_payload`` is not ``None``,
        ``job_kind`` is ``None`` and the dict is an ERROR tool response.
    """
    job_service = _resolve_job_service(runtime)
    if job_service is None:
        return None, None
    try:
        status = job_service.store.read_status(job_id)
    except OSError as exc:
        logger.warning("Failed to read job status for {}: {}", job_id, exc)
        return None, None
    if not isinstance(status, dict) or not str(status.get("job_id") or job_id).strip():
        return None, None
    return _job_kind_from_status(status), None


def _require_job_kind(
    *,
    runtime: Any,
    job_id: str,
    expected_kind: str,
    tool_name: str,
    alternate_tool: str,
) -> dict[str, Any] | None:
    """Return an ERROR payload when ``job_id`` does not belong to ``expected_kind``.

    Args:
        runtime: Active runtime exposing job services.
        job_id: Opaque job id from a submit tool.
        expected_kind: Required track value (``resource`` or ``subagent``).
        tool_name: Current tool name used in the error message.
        alternate_tool: Suggested counterpart tool for the other track.

    Returns:
        ERROR dict when the track mismatches; otherwise ``None``.
    """
    job_kind, error = _read_job_kind(runtime, job_id)
    if error is not None:
        return error
    if job_kind is None:
        return None
    if job_kind == expected_kind:
        return None
    return {
        "status": "ERROR",
        "message": (f"job_id {job_id} is a {job_kind} job; use {alternate_tool} instead of {tool_name}."),
    }


def _resource_collect_frontend_msg(payload: dict[str, Any]) -> str:
    """Build a user-facing collect summary for one resource job payload.

    Args:
        payload: Raw or partially enriched collect payload.

    Returns:
        Multi-line summary string for tool consumers.
    """
    summary = str(payload.get("summary") or "").strip()
    error = str(payload.get("error") or "").strip()
    terminal_status = str(payload.get("status") or "").strip().lower()
    lines: list[str] = []

    if terminal_status == "completed":
        lines.append(summary or "Resource job completed.")
    elif terminal_status in TERMINAL_STATUSES:
        lines.append(error or summary or f"Resource job {terminal_status}.")
    else:
        lines.append(summary or error or f"Resource job status: {terminal_status or 'unknown'}.")

    outputs = payload.get("outputs")
    if isinstance(outputs, list):
        for item in outputs:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            if kind == "clickhouse_result":
                columns = item.get("columns") if isinstance(item.get("columns"), list) else []
                rows = item.get("rows") if isinstance(item.get("rows"), list) else []
                row_count = int(item.get("row_count") or len(rows))
                lines.append(f"columns={columns}")
                lines.append(f"row_count={row_count}")
                if rows:
                    preview = json.dumps(rows[:10], ensure_ascii=False)
                    lines.append(f"rows={preview}")
            elif item.get("text"):
                lines.append(str(item.get("text")))
    return "\n".join(line for line in lines if line)


def _resource_collect_original_msg(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-serializable collect snapshot for LLM reasoning.

    Args:
        payload: Raw or partially enriched collect payload.

    Returns:
        Payload fields excluding message aliases to avoid circular references.
    """
    return {key: value for key, value in payload.items() if key not in {"original_msg", "frontend_msg"}}


def _enrich_resource_collect_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Map resource job collect fields into ``original_msg`` / ``frontend_msg``.

    Args:
        payload: Raw collect payload from :class:`ResourceJobCoordinator`.

    Returns:
        The same payload with LLM/frontend-friendly message fields when missing.
    """
    if str(payload.get("status") or "").strip().upper() == "ERROR":
        return payload
    enriched = dict(payload)
    frontend = _resource_collect_frontend_msg(enriched)
    if enriched.get("original_msg") is None:
        enriched["original_msg"] = _resource_collect_original_msg(enriched)
    if not str(enriched.get("frontend_msg") or "").strip():
        enriched["frontend_msg"] = frontend
    return enriched


def _poll_with_watch(
    *,
    job_id: str,
    cursor: str,
    event_limit: int,
    watch_sec: int,
    interval_sec: float,
    stop_on_terminal: bool,
    runtime: Any,
    poll: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Poll one job once or in watch mode until terminal or timeout.

    Args:
        job_id: Opaque job id from a submit tool.
        cursor: Optional event cursor from a previous poll.
        event_limit: Maximum events returned per poll call.
        watch_sec: When positive, keep polling up to this many seconds.
        interval_sec: Sleep interval between watch polls.
        stop_on_terminal: Stop watch mode on terminal job status.
        runtime: Active :class:`~dataagent.core.cbb.runtime.Runtime`.
        poll: Bound ``poll(job_id=..., cursor=..., event_limit=...)`` callable.

    Returns:
        Latest poll snapshot, optionally including a ``watch`` block.
    """
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        return {"status": "ERROR", "message": "job_id is required"}

    normalized_limit = max(1, min(POLL_WATCH_MAX_EVENT_LIMIT, int(event_limit or POLL_WATCH_DEFAULT_EVENT_LIMIT)))
    normalized_watch = max(0, min(POLL_WATCH_MAX_WATCH_SEC, int(watch_sec or 0)))
    normalized_interval = max(
        POLL_WATCH_MIN_INTERVAL_SEC,
        min(POLL_WATCH_MAX_INTERVAL_SEC, float(interval_sec or POLL_WATCH_DEFAULT_INTERVAL_SEC)),
    )
    if normalized_watch <= 0:
        return poll(
            job_id=normalized_job_id,
            cursor=str(cursor or "") or None,
            event_limit=normalized_limit,
        )

    deadline = time.monotonic() + normalized_watch
    next_cursor = str(cursor or "") or None
    snapshots: list[dict[str, Any]] = []
    latest: dict[str, Any] = {}
    while True:
        latest = poll(
            job_id=normalized_job_id,
            cursor=next_cursor,
            event_limit=normalized_limit,
        )
        snapshots.append(latest)
        next_cursor = str(latest.get("cursor") or next_cursor or "")
        status = str(latest.get("status") or "").strip().lower()
        if bool(stop_on_terminal) and status in TERMINAL_STATUSES:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        runtime.ensure_not_cancelled()
        time.sleep(min(normalized_interval, remaining))

    latest = dict(latest)
    latest["watch"] = {
        "enabled": True,
        "watch_sec": normalized_watch,
        "interval_sec": normalized_interval,
        "snapshots": snapshots,
    }
    return latest


def _resolve_workspace_file(file_path: str, workspace_dir: Path) -> Path | None:
    """Resolve a workspace-relative (or absolute) file path inside the workspace.

    Args:
        file_path: A raw path string from the tool argument.
        workspace_dir: Absolute workspace root directory.

    Returns:
        Resolved absolute :class:`Path` to an existing file, or ``None`` when
        the path is outside the workspace or does not point to a regular file.
    """
    raw_path = str(file_path or "").strip()
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


# ---------------------------------------------------------------------------
# Subagent job tools
# ---------------------------------------------------------------------------


def submit_subagent(
    agent_id: str,
    task: str,
    timeout_sec: int = DEFAULT_SUBMIT_SUBAGENT_TIMEOUT_SEC,
    workspace_rel_path: str | None = None,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Submit an asynchronous subagent job.

    Use this tool to delegate bounded work to a registered specialist subagent.
    Poll with ``poll_subagent`` and collect the final payload with ``collect_subagent``.

    To continue in an existing subagent workspace, pass ``workspace_rel_path`` from a
    prior ``submit_subagent`` / ``collect_subagent`` response (for example
    ``subagents/{id}``). Omit it to allocate a fresh workspace.

    Args:
        agent_id: Registered specialist id from ``SUBAGENT_CONFIGS``.
        task: Task description forwarded to the subagent.
        timeout_sec: Job timeout in seconds.
        workspace_rel_path: Optional relative path under the parent workspace for reuse.
        _tool_context: Injected runtime/config context (not visible to the LLM).
    """
    runtime = _tool_context.runtime
    if runtime is None:
        return {"status": "ERROR", "message": "submit_subagent requires a mounted runtime."}
    agent_service = runtime.ensure_job_services()
    if agent_service is None:
        return {"status": "ERROR", "message": "submit_subagent requires a resolved parent workspace."}
    return agent_service.submit(
        agent_id=str(agent_id or "").strip(),
        task=str(task or ""),
        timeout_sec=int(timeout_sec or DEFAULT_SUBMIT_SUBAGENT_TIMEOUT_SEC),
        job_envelope=envelope_from_tool_context(_tool_context) or None,
    )


def poll_subagent(
    job_id: str,
    cursor: str = "",
    event_limit: int = POLL_WATCH_DEFAULT_EVENT_LIMIT,
    watch_sec: int = 0,
    interval_sec: float = POLL_WATCH_DEFAULT_INTERVAL_SEC,
    stop_on_terminal: bool = True,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Poll an asynchronous subagent job.

    Args:
        job_id: The job id returned by ``submit_subagent``.
        cursor: Optional event cursor returned by the previous poll.
        event_limit: Maximum number of job events to return per poll.
        watch_sec: When greater than 0, keep polling for up to this many seconds.
        interval_sec: Polling interval used by watch mode.
        stop_on_terminal: Stop watch mode when the job reaches a terminal status.
        _tool_context: Injected runtime context (not visible to the LLM).
    """
    runtime = _tool_context.runtime
    if runtime is None:
        return {"status": "ERROR", "message": "poll_subagent requires a mounted runtime."}
    agent_service = runtime.ensure_job_services()
    if agent_service is None:
        return {"status": "ERROR", "message": "poll_subagent requires a resolved parent workspace."}
    normalized_job_id = str(job_id or "").strip()
    kind_error = _require_job_kind(
        runtime=runtime,
        job_id=normalized_job_id,
        expected_kind=_SUBAGENT_JOB_KIND,
        tool_name="poll_subagent",
        alternate_tool="poll_job",
    )
    if kind_error is not None:
        return kind_error
    return _poll_with_watch(
        job_id=job_id,
        cursor=cursor,
        event_limit=event_limit,
        watch_sec=watch_sec,
        interval_sec=interval_sec,
        stop_on_terminal=stop_on_terminal,
        runtime=runtime,
        poll=agent_service.poll,
    )


def collect_subagent(job_id: str, *, _tool_context: ToolExecutionContext) -> dict[str, Any]:
    """Collect the final result of an asynchronous subagent job.

    Args:
        job_id: The job id returned by ``submit_subagent``.
        _tool_context: Injected runtime context (not visible to the LLM).
    """
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        return {"status": "ERROR", "message": "job_id is required"}
    runtime = _tool_context.runtime
    if runtime is None:
        return {"status": "ERROR", "message": "collect_subagent requires a mounted runtime."}
    agent_service = runtime.ensure_job_services()
    if agent_service is None:
        return {"status": "ERROR", "message": "collect_subagent requires a resolved parent workspace."}
    kind_error = _require_job_kind(
        runtime=runtime,
        job_id=normalized_job_id,
        expected_kind=_SUBAGENT_JOB_KIND,
        tool_name="collect_subagent",
        alternate_tool="collect_job",
    )
    if kind_error is not None:
        return kind_error
    return agent_service.collect(job_id=normalized_job_id)


def cancel_subagent(job_id: str, *, _tool_context: ToolExecutionContext) -> dict[str, Any]:
    """Cancel an asynchronous subagent job.

    Args:
        job_id: The job id returned by ``submit_subagent``.
        _tool_context: Injected runtime context (not visible to the LLM).
    """
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        return {"status": "ERROR", "message": "job_id is required"}
    runtime = _tool_context.runtime
    if runtime is None:
        return {"status": "ERROR", "message": "cancel_subagent requires a mounted runtime."}
    agent_service = runtime.ensure_job_services()
    if agent_service is None:
        return {"status": "ERROR", "message": "cancel_subagent requires a resolved parent workspace."}
    kind_error = _require_job_kind(
        runtime=runtime,
        job_id=normalized_job_id,
        expected_kind=_SUBAGENT_JOB_KIND,
        tool_name="cancel_subagent",
        alternate_tool="cancel_job",
    )
    if kind_error is not None:
        return kind_error
    return agent_service.cancel(job_id=normalized_job_id)


# ---------------------------------------------------------------------------
# Resource job tools
# ---------------------------------------------------------------------------


def submit_resource_job(
    command: str = "",
    command_file: str = "",
    task_type: str = "resource",
    resource_id: str = "",
    timeout_sec: int = DEFAULT_RESOURCE_JOB_TIMEOUT_SEC,
    script_artifact: dict[str, Any] | None = None,
    inputs: list[dict[str, Any]] | None = None,
    outputs: list[dict[str, Any]] | None = None,
    receipt_ids: list[str] | None = None,
    out_kind: str = "",
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Submit an asynchronous resource job.

    Use this tool to run bounded shell work on a configured compute resource.
    Poll with ``poll_job`` and collect the final payload with ``collect_job``.

    For long commands (for example complex SQL statements) that may exceed LLM
    tool-call argument limits, write the command content to a file in the
    workspace first with ``write_file``, then pass its workspace-relative path as
    ``command_file``.  ``command`` and ``command_file`` are mutually exclusive.

    Args:
        command: Shell command executed in the workspace sandbox.
        command_file: Workspace-relative path to a file whose content is used
            as the shell command. Provide EITHER ``command`` OR ``command_file``,
            not both. Prefer ``command_file`` when the command is very long (for
            example a multi-table SQL statement).
        task_type: Task type used for consumption lookup (for example ``resource`` or ``batch_task``).
        resource_id: Optional explicit resource id from ``RESOURCES``.
        timeout_sec: Job timeout in seconds.
        script_artifact: Optional script artifact metadata with a workspace-relative ``path``.
        inputs: Optional structured input descriptors.
        outputs: Optional structured output descriptors.
        receipt_ids: Optional workflow receipt ids (reserved for future phases).
        out_kind: Optional output kind hint.
        _tool_context: Injected runtime context (not visible to the LLM).
    """
    # ── resolve command_file → command_str ───────────────────────────────
    command_str = str(command or "").strip()
    command_file_str = str(command_file or "").strip()
    if command_str and command_file_str:
        return {
            "status": "ERROR",
            "message": "submit_resource_job: command and command_file are mutually exclusive. Provide one or the other.",
        }

    runtime = _tool_context.runtime
    if runtime is None:
        return {"status": "ERROR", "message": "submit_resource_job requires a mounted runtime."}

    if command_file_str:
        workspace_dir = getattr(runtime, "workspace_dir", None)
        if not workspace_dir:
            return {"status": "ERROR", "message": "submit_resource_job cannot resolve workspace_dir for command_file."}
        resolved_path = _resolve_workspace_file(command_file_str, Path(str(workspace_dir)))
        if resolved_path is None:
            return {
                "status": "ERROR",
                "message": f"command_file must point to a file inside the workspace: {command_file_str}",
            }
        try:
            command_str = resolved_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return {"status": "ERROR", "message": f"Failed to read command_file: {exc}"}
        if not command_str:
            return {"status": "ERROR", "message": "command_file is empty; write the command content to the file first."}

    if not command_str:
        return {"status": "ERROR", "message": "submit_resource_job requires command or command_file."}

    coordinator = runtime.ensure_resource_coordinator()
    if coordinator is None:
        return {"status": "ERROR", "message": "submit_resource_job requires RESOURCES configuration."}
    return coordinator.submit_job(
        resource_id=str(resource_id or "").strip(),
        command=command_str,
        task_type=str(task_type or "resource").strip() or "resource",
        timeout_sec=int(timeout_sec or DEFAULT_RESOURCE_JOB_TIMEOUT_SEC),
        script_artifact=script_artifact,
        inputs=inputs,
        outputs=outputs,
        receipt_ids=receipt_ids,
        out_kind=str(out_kind or "").strip(),
        job_envelope=envelope_from_tool_context(_tool_context) or None,
    )


def poll_job(
    job_id: str,
    cursor: str = "",
    event_limit: int = POLL_WATCH_DEFAULT_EVENT_LIMIT,
    watch_sec: int = 0,
    interval_sec: float = POLL_WATCH_DEFAULT_INTERVAL_SEC,
    stop_on_terminal: bool = True,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Poll an asynchronous resource job.

    Args:
        job_id: The job id returned by ``submit_resource_job``.
        cursor: Optional event cursor returned by the previous poll.
        event_limit: Maximum number of job events to return per poll.
        watch_sec: When greater than 0, keep polling for up to this many seconds.
        interval_sec: Polling interval used by watch mode.
        stop_on_terminal: Stop watch mode when the job reaches a terminal status.
        _tool_context: Injected runtime context (not visible to the LLM).
    """
    runtime = _tool_context.runtime
    if runtime is None:
        return {"status": "ERROR", "message": "poll_job requires a mounted runtime."}
    coordinator = runtime.ensure_resource_coordinator()
    if coordinator is None:
        return {"status": "ERROR", "message": "poll_job requires RESOURCES configuration."}
    normalized_job_id = str(job_id or "").strip()
    kind_error = _require_job_kind(
        runtime=runtime,
        job_id=normalized_job_id,
        expected_kind=_RESOURCE_JOB_KIND,
        tool_name="poll_job",
        alternate_tool="poll_subagent",
    )
    if kind_error is not None:
        return kind_error
    return _poll_with_watch(
        job_id=job_id,
        cursor=cursor,
        event_limit=event_limit,
        watch_sec=watch_sec,
        interval_sec=interval_sec,
        stop_on_terminal=stop_on_terminal,
        runtime=runtime,
        poll=coordinator.poll,
    )


def collect_job(job_id: str, *, _tool_context: ToolExecutionContext) -> dict[str, Any]:
    """Collect the final result of an asynchronous resource job.

    Args:
        job_id: The job id returned by ``submit_resource_job``.
        _tool_context: Injected runtime context (not visible to the LLM).
    """
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        return {"status": "ERROR", "message": "job_id is required"}
    runtime = _tool_context.runtime
    if runtime is None:
        return {"status": "ERROR", "message": "collect_job requires a mounted runtime."}
    coordinator = runtime.ensure_resource_coordinator()
    if coordinator is None:
        return {"status": "ERROR", "message": "collect_job requires RESOURCES configuration."}
    kind_error = _require_job_kind(
        runtime=runtime,
        job_id=normalized_job_id,
        expected_kind=_RESOURCE_JOB_KIND,
        tool_name="collect_job",
        alternate_tool="collect_subagent",
    )
    if kind_error is not None:
        return kind_error
    return _enrich_resource_collect_payload(coordinator.collect(job_id=normalized_job_id))


def cancel_job(job_id: str, *, _tool_context: ToolExecutionContext) -> dict[str, Any]:
    """Cancel an asynchronous resource job.

    Args:
        job_id: The job id returned by ``submit_resource_job``.
        _tool_context: Injected runtime context (not visible to the LLM).
    """
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        return {"status": "ERROR", "message": "job_id is required"}
    runtime = _tool_context.runtime
    if runtime is None:
        return {"status": "ERROR", "message": "cancel_job requires a mounted runtime."}
    coordinator = runtime.ensure_resource_coordinator()
    if coordinator is None:
        return {"status": "ERROR", "message": "cancel_job requires RESOURCES configuration."}
    kind_error = _require_job_kind(
        runtime=runtime,
        job_id=normalized_job_id,
        expected_kind=_RESOURCE_JOB_KIND,
        tool_name="cancel_job",
        alternate_tool="cancel_subagent",
    )
    if kind_error is not None:
        return kind_error
    return coordinator.cancel(job_id=normalized_job_id)


def list_resources(*, _tool_context: ToolExecutionContext) -> dict[str, Any]:
    """List configured resources with capacity and runtime usage.

    Returns executable and non-executable catalog entries. Non-executable
    resources are visible for discovery but cannot be submitted via
    ``submit_resource_job``.

    Args:
        _tool_context: Injected runtime context (not visible to the LLM).
    """
    runtime = _tool_context.runtime
    if runtime is None:
        return {"status": "ERROR", "message": "list_resources requires a mounted runtime."}
    coordinator = runtime.ensure_resource_coordinator()
    if coordinator is None:
        return {"status": "ERROR", "message": "list_resources requires RESOURCES configuration."}
    return coordinator.list_resources()
