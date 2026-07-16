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
"""Local Flex subagent adapter for Job-path execution."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Any

from dataagent.core.agents.registry import AgentSpec
from dataagent.core.agents.subagent_subprocess_runner import (
    SubagentSubprocessRunner,
    derive_job_sub_id,
)
from dataagent.core.jobs.models import JobResult
from dataagent.core.workspace.publish import (
    ensure_subagent_output_root,
    list_published_artifacts,
    publish_subagent_artifacts,
)
from dataagent.utils.constants import DEFAULT_SUBMIT_SUBAGENT_TIMEOUT_SEC
from dataagent.utils.runtime_paths import is_subagent_output_sharing_enabled


class LocalFlexAdapter:
    """Run a registered Flex subagent via ``sub_agent_entry`` in a background job."""

    def __init__(self, *, runner: SubagentSubprocessRunner | None = None) -> None:
        """Create an adapter with an optional shared subprocess runner."""
        self._runner = runner or SubagentSubprocessRunner()

    def run(
        self,
        *,
        job_id: str,
        spec: AgentSpec,
        task: str,
        workspace_dir: Path,
        subagent_session_id: str,
        workspace_rel_path: str = "",
        runtime: Any,
        cancel_event: Event,
        emit_event: Callable[[dict[str, Any]], None],
        parent_tool_call_id: str = "",
        reuse_workspace: bool = False,
        timeout_sec: int = DEFAULT_SUBMIT_SUBAGENT_TIMEOUT_SEC,
    ) -> JobResult:
        """Execute one subagent job synchronously inside a JobService worker thread."""
        started_at = time.monotonic()
        emit_event(
            {
                "type": "agent_start",
                "job_id": job_id,
                "agent_id": spec.id,
                "status": "running",
                "parent_tool_call_id": parent_tool_call_id,
                "workspace_dir": str(Path(workspace_dir).resolve()),
                "subagent_session_id": subagent_session_id,
            }
        )
        if cancel_event.is_set():
            return JobResult(
                job_id=job_id,
                agent_id=spec.id,
                status="cancelled",
                summary="Agent job cancelled.",
                subagent_session_id=subagent_session_id,
                workspace_rel_path=workspace_rel_path,
            )

        user_id = str(getattr(runtime, "user_id", "") or "anonymous")
        parent_session_id = str(getattr(runtime, "session_id", "") or "default_session")
        parent_workspace_dir = Path(getattr(runtime, "workspace_dir", "")).expanduser().resolve()
        runtime_config = runtime.get_all_config() if callable(getattr(runtime, "get_all_config", None)) else None
        output_sharing_enabled = is_subagent_output_sharing_enabled(runtime_config)
        subagent_output_dir = (
            ensure_subagent_output_root(parent_workspace=parent_workspace_dir, config=runtime_config)
            if output_sharing_enabled
            else None
        )
        timeout = max(1, int(timeout_sec or DEFAULT_SUBMIT_SUBAGENT_TIMEOUT_SEC))
        sub_id = derive_job_sub_id(subagent_session_id)
        progress_callback = getattr(runtime, "on_subagent_progress", None)
        sandbox = runtime.sandbox

        try:
            outcome = asyncio.run(
                self._runner.run(
                    query=task,
                    config_path=spec.config_path,
                    workspace_dir=Path(workspace_dir),
                    subagent_output_dir=subagent_output_dir,
                    subagent_session_id=subagent_session_id,
                    user_id=user_id,
                    parent_session_id=parent_session_id,
                    sub_id=sub_id,
                    timeout=timeout,
                    sandbox=sandbox,
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                    reuse_workspace=reuse_workspace,
                )
            )
        except Exception as exc:
            return JobResult(
                job_id=job_id,
                agent_id=spec.id,
                status="failed",
                summary=f"Agent failed: {exc}",
                error=str(exc),
                subagent_session_id=subagent_session_id,
                workspace_rel_path=workspace_rel_path,
                metrics={"duration_ms": int((time.monotonic() - started_at) * 1000)},
            )

        if cancel_event.is_set():
            status = "cancelled"
            summary = "Agent job cancelled."
        else:
            status = outcome.status if outcome.status in {"failed", "cancelled", "timed_out"} else "completed"
            summary = outcome.frontend_msg or ("Agent job cancelled." if status == "cancelled" else "")

        published_path = ""
        published_artifacts: list[str] = []
        if status == "completed" and output_sharing_enabled:
            try:
                published = publish_subagent_artifacts(
                    source_workspace=workspace_dir,
                    parent_workspace=parent_workspace_dir,
                    subagent_session_id=subagent_session_id,
                    agent_id=spec.id,
                    task=task,
                    job_id=job_id,
                    config=runtime_config,
                )
                published_path = str(published)
                published_artifacts = list_published_artifacts(published)
            except Exception as exc:
                status = "failed"
                summary = f"Subagent artifact publish failed: {exc}"
                outcome = outcome.__class__(
                    original_msg=outcome.original_msg,
                    frontend_msg=outcome.frontend_msg,
                    state=outcome.state,
                    status="failed",
                    error=str(exc),
                )

        emit_event(
            {
                "type": "agent_step",
                "job_id": job_id,
                "agent_id": spec.id,
                "status": status,
                "parent_tool_call_id": parent_tool_call_id,
            }
        )
        return JobResult(
            job_id=job_id,
            agent_id=spec.id,
            status=status,
            summary=summary,
            error=outcome.error,
            original_msg=outcome.original_msg,
            frontend_msg=outcome.frontend_msg,
            state=outcome.state,
            subagent_session_id=subagent_session_id,
            workspace_rel_path=workspace_rel_path,
            published_path=published_path,
            published_artifacts=published_artifacts,
            metrics={"duration_ms": int((time.monotonic() - started_at) * 1000)},
        )
