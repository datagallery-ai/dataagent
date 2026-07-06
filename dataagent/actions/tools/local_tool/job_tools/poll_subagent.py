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
"""Poll an asynchronous subagent job."""

from __future__ import annotations

import time
from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.jobs.models import TERMINAL_STATUSES
from dataagent.utils.constants import (
    POLL_WATCH_DEFAULT_EVENT_LIMIT,
    POLL_WATCH_DEFAULT_INTERVAL_SEC,
    POLL_WATCH_MAX_EVENT_LIMIT,
    POLL_WATCH_MAX_INTERVAL_SEC,
    POLL_WATCH_MAX_WATCH_SEC,
    POLL_WATCH_MIN_INTERVAL_SEC,
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
    normalized_job_id = str(job_id or "").strip()
    if not normalized_job_id:
        return {"status": "ERROR", "message": "job_id is required"}
    runtime = _tool_context.runtime
    if runtime is None:
        return {"status": "ERROR", "message": "poll_subagent requires a mounted runtime."}
    agent_service = runtime.ensure_job_services()
    if agent_service is None:
        return {"status": "ERROR", "message": "poll_subagent requires a resolved parent workspace."}

    normalized_limit = max(1, min(POLL_WATCH_MAX_EVENT_LIMIT, int(event_limit or POLL_WATCH_DEFAULT_EVENT_LIMIT)))
    normalized_watch = max(0, min(POLL_WATCH_MAX_WATCH_SEC, int(watch_sec or 0)))
    normalized_interval = max(
        POLL_WATCH_MIN_INTERVAL_SEC,
        min(POLL_WATCH_MAX_INTERVAL_SEC, float(interval_sec or POLL_WATCH_DEFAULT_INTERVAL_SEC)),
    )
    if normalized_watch <= 0:
        return agent_service.poll(
            job_id=normalized_job_id,
            cursor=str(cursor or "") or None,
            event_limit=normalized_limit,
        )

    deadline = time.monotonic() + normalized_watch
    next_cursor = str(cursor or "") or None
    snapshots: list[dict[str, Any]] = []
    latest: dict[str, Any] = {}
    while True:
        latest = agent_service.poll(
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
