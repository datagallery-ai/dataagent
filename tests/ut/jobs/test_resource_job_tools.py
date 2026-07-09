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
"""System-style tests for resource job lifecycle tools (no LLM)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

from dataagent.actions.resources.bootstrap import (
    default_mcp_client_factory,
    default_resource_operation_registry,
)
from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.job_tools import (
    cancel_job,
    collect_job,
    poll_job,
    poll_subagent,
    submit_resource_job,
)
from dataagent.actions.tools.local_tool.sandbox import NoopSandbox
from dataagent.core.agents.registry import AgentRegistry
from dataagent.core.agents.service import AgentService
from dataagent.core.jobs.file_store import FileJobStore
from dataagent.core.jobs.models import JobResult
from dataagent.core.jobs.service import JobService
from dataagent.core.resources.registry import ResourceRegistry
from dataagent.core.resources.service import ResourceService


def _resources_config() -> dict[str, Any]:
    """Minimal RESOURCES config for tool-level tests."""
    return {
        "RESOURCES": [
            {
                "id": "local",
                "category": "executable",
                "transport": {"type": "local"},
                "operations": {
                    "submit": "sandbox.submit",
                    "poll": "sandbox.poll",
                    "collect": "sandbox.collect",
                    "cancel": "sandbox.cancel",
                },
                "capacity": {"total": 4, "unit": "slot"},
                "consumption": {"*": 1},
            }
        ]
    }


def _build_tool_context(tmp_path: Path) -> ToolExecutionContext:
    """Bind resource lifecycle tools to a workspace-scoped ResourceService."""
    parent_ws = tmp_path / "parent_session"
    parent_ws.mkdir(parents=True, exist_ok=True)
    store = FileJobStore(parent_ws)
    job_service = JobService(store)
    runtime = SimpleNamespace(
        workspace_dir=parent_ws,
        sandbox=NoopSandbox(workspace_root=parent_ws),
        ensure_not_cancelled=lambda: None,
    )
    registry = ResourceRegistry.from_config(_resources_config())
    resource_service = ResourceService(
        registry=registry,
        job_service=job_service,
        runtime=runtime,
        operation_registry=default_resource_operation_registry(),
        mcp_client_factory=default_mcp_client_factory(),
    )
    agent_service = AgentService(registry=AgentRegistry(), job_service=job_service, runtime=runtime)
    runtime.ensure_resource_services = lambda: resource_service
    runtime.ensure_job_services = lambda: agent_service
    return ToolExecutionContext(runtime=runtime)


def _wait_until_terminal(job_service: JobService, job_id: str, *, timeout_sec: float = 5.0) -> str:
    """Poll until the job reaches a terminal status or timeout."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        snap = job_service.poll(job_id)
        if snap.status in {"completed", "failed", "cancelled", "timed_out"}:
            return snap.status
        time.sleep(0.05)
    return job_service.poll(job_id).status


def test_resource_job_tools_submit_poll_collect_chain(tmp_path: Path) -> None:
    """submit_resource_job → poll_job → collect_job returns completed summary."""
    ctx = _build_tool_context(tmp_path)
    runtime = ctx.runtime
    submit_payload = submit_resource_job(
        command="echo tool_chain",
        task_type="resource",
        resource_id="local",
        _tool_context=ctx,
    )
    assert submit_payload["status"] == "queued"
    job_id = str(submit_payload["job_id"])

    poll_payload = poll_job(
        job_id=job_id,
        watch_sec=5,
        interval_sec=0.05,
        _tool_context=ctx,
    )
    assert poll_payload["status"] == "completed"

    collected = collect_job(job_id=job_id, _tool_context=ctx)
    assert collected["status"] == "completed"
    assert "tool_chain" in str(collected.get("summary") or "")
    assert isinstance(collected.get("original_msg"), dict)
    assert collected["original_msg"].get("summary") == "tool_chain"
    assert "original_msg" not in collected["original_msg"]
    assert json.dumps(collected, ensure_ascii=False)
    assert "tool_chain" in str(collected.get("frontend_msg") or "")
    assert (runtime.workspace_dir / "jobs" / job_id / "job.json").is_file()
    assert (runtime.workspace_dir / "jobs" / job_id / "result.json").is_file()


def test_poll_job_rejects_subagent_job_id(tmp_path: Path) -> None:
    """poll_job must not operate on subagent-track jobs."""
    ctx = _build_tool_context(tmp_path)
    runtime = ctx.runtime
    job_service = runtime.ensure_resource_services().job_service
    handle = job_service.start(
        agent_id="echo_ref",
        task="hello",
        runner=lambda job_id, cancel_event: JobResult(job_id=job_id, agent_id="echo_ref", status="completed"),
        allocation={"agent": {"pool": "local"}},
        metadata={"subagent_session_id": "sub-1"},
    )
    denied = poll_job(job_id=handle["job_id"], _tool_context=ctx)
    assert denied["status"] == "ERROR"
    assert "poll_subagent" in denied["message"]


def test_poll_subagent_rejects_resource_job_id(tmp_path: Path) -> None:
    """poll_subagent must not operate on resource-track jobs."""
    ctx = _build_tool_context(tmp_path)
    submit_payload = submit_resource_job(
        command="echo blocked",
        task_type="resource",
        resource_id="local",
        _tool_context=ctx,
    )
    denied = poll_subagent(job_id=str(submit_payload["job_id"]), _tool_context=ctx)
    assert denied["status"] == "ERROR"
    assert "poll_job" in denied["message"]


def test_cancel_job_tool_terminates_running_resource_job(tmp_path: Path) -> None:
    """cancel_job stops a long-running resource job."""
    ctx = _build_tool_context(tmp_path)
    runtime = ctx.runtime
    resource_service = runtime.ensure_resource_services()
    started = Event()

    def slow_runner(job_id: str, cancel_event: Event) -> JobResult:
        started.set()
        while not cancel_event.is_set():
            time.sleep(0.05)
        return JobResult(
            job_id=job_id,
            agent_id="resource:local",
            status="cancelled",
            summary="Resource job cancelled.",
        )

    handle = resource_service.job_service.start(
        agent_id="resource:local",
        task="block",
        runner=slow_runner,
        metadata={"job_kind": "resource", "resource_id": "local"},
        allocation={"resource": {"id": "local", "task_type": "resource", "amount": 1, "unit": "slot"}},
    )
    job_id = handle["job_id"]
    assert started.wait(timeout=2.0)
    cancel_payload = cancel_job(job_id=job_id, _tool_context=ctx)
    assert cancel_payload["status"] == "cancelled"
    assert _wait_until_terminal(resource_service.job_service, job_id) == "cancelled"
