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
"""Mock remote compute-pool MCP server for Resource Job B2 manual testing.

Exposes Galatea-compatible lifecycle tools:

- ``submit_job``
- ``poll_job``
- ``collect_job``
- ``cancel_job``

Run (streamable HTTP, default ``http://127.0.0.1:8765/mcp``)::

    cd /data1/xzx/ferry
    .venv/bin/python -m dataagent.actions.tools.mcp_tool.mock_compute_pool

Pair with Agent ``RESOURCES``::

    transport:
      type: mcp
      url: http://127.0.0.1:8765/mcp
    operations:
      submit: submit_job
      poll: poll_job
      collect: collect_job
      cancel: cancel_job
"""

from __future__ import annotations

import argparse
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from mcp.server.fastmcp import FastMCP

_POLLS_BEFORE_COMPLETE = 2


@dataclass
class _RemoteJob:
    """In-memory remote job state for the mock compute pool."""

    job_id: str
    command: str
    resource_id: str
    task_type: str
    poll_count: int = 0
    cancelled: bool = False
    created_at: float = field(default_factory=time.time)


class _JobStore:
    """Thread-safe store backing the mock MCP compute pool."""

    def __init__(self) -> None:
        """Create an empty remote job store."""
        self._jobs: dict[str, _RemoteJob] = {}
        self._lock = threading.Lock()

    def create(self, *, command: str, resource_id: str, task_type: str) -> _RemoteJob:
        """Create one remote job and return its metadata."""
        job_id = f"remote-{uuid.uuid4().hex[:12]}"
        job = _RemoteJob(
            job_id=job_id,
            command=command,
            resource_id=resource_id,
            task_type=task_type,
        )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> _RemoteJob | None:
        """Return one remote job by id."""
        with self._lock:
            return self._jobs.get(str(job_id or "").strip())

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Mark one remote job as cancelled."""
        job = self.get(job_id)
        if job is None:
            return {"status": "error", "error": f"job not found: {job_id}"}
        job.cancelled = True
        return {"status": "cancelled", "job_id": job.job_id}

    def poll(self, job_id: str) -> dict[str, Any]:
        """Advance and return one remote job status."""
        job = self.get(job_id)
        if job is None:
            return {"status": "error", "error": f"job not found: {job_id}"}
        if job.cancelled:
            return {"status": "cancelled", "job_id": job.job_id}
        job.poll_count += 1
        if job.poll_count < _POLLS_BEFORE_COMPLETE:
            return {"status": "running", "job_id": job.job_id, "poll_count": job.poll_count}
        return {"status": "completed", "job_id": job.job_id, "poll_count": job.poll_count}

    def collect(self, job_id: str) -> dict[str, Any]:
        """Return the terminal payload for one remote job."""
        job = self.get(job_id)
        if job is None:
            return {"status": "error", "error": f"job not found: {job_id}", "exit_code": 1}
        if job.cancelled:
            return {
                "status": "cancelled",
                "job_id": job.job_id,
                "summary": f"cancelled remote job for: {job.command}",
                "exit_code": 130,
            }
        summary = (
            f"mock compute pool executed command={job.command!r} "
            f"resource_id={job.resource_id} task_type={job.task_type}"
        )
        return {
            "status": "completed",
            "job_id": job.job_id,
            "summary": summary,
            "exit_code": 0,
            "outputs": [{"kind": "stdout", "text": summary}],
        }


_STORE = _JobStore()


def _command_from_envelope(envelope: dict[str, Any]) -> str:
    """Extract the shell command from a resource job envelope."""
    return str(envelope.get("command") or "").strip()


def _allocation_fields(allocation: dict[str, Any]) -> tuple[str, str]:
    """Extract resource id and task type from an allocation payload."""
    resource_id = str(allocation.get("id") or "").strip()
    task_type = str(allocation.get("task_type") or "").strip()
    return resource_id, task_type


def create_server(*, host: str, port: int) -> FastMCP:
    """Build the mock compute-pool FastMCP application."""
    server = FastMCP(
        "Mock Compute Pool",
        instructions="Galatea-compatible resource lifecycle MCP server for Ferry B2 testing.",
        host=host,
        port=port,
        streamable_http_path="/mcp",
        stateless_http=True,
    )

    @server.tool()
    def submit_job(envelope: dict[str, Any], allocation: dict[str, Any]) -> dict[str, Any]:
        """Submit one asynchronous resource job to the mock compute pool."""
        command = _command_from_envelope(envelope)
        resource_id, task_type = _allocation_fields(allocation)
        if not command:
            return {"status": "error", "error": "envelope.command is required", "exit_code": 1}
        job = _STORE.create(command=command, resource_id=resource_id, task_type=task_type)
        logger.info(
            "submit_job remote_id={} command={!r} resource_id={} task_type={}",
            job.job_id,
            command,
            resource_id,
            task_type,
        )
        return {"status": "running", "job_id": job.job_id}

    @server.tool()
    def poll_job(job_id: str) -> dict[str, Any]:
        """Poll one remote job until it reaches a terminal state."""
        payload = _STORE.poll(job_id)
        logger.info("poll_job job_id={} status={}", job_id, payload.get("status"))
        return payload

    @server.tool()
    def collect_job(job_id: str) -> dict[str, Any]:
        """Collect the terminal result for one remote job."""
        payload = _STORE.collect(job_id)
        logger.info("collect_job job_id={} status={}", job_id, payload.get("status"))
        return payload

    @server.tool()
    def cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel one running remote job."""
        payload = _STORE.cancel(job_id)
        logger.info("cancel_job job_id={} status={}", job_id, payload.get("status"))
        return payload

    return server


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the mock server."""
    parser = argparse.ArgumentParser(description="Run the mock compute-pool MCP server.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    return parser.parse_args()


def main() -> None:
    """Start the mock compute-pool MCP server."""
    args = _parse_args()
    server = create_server(host=args.host, port=args.port)
    endpoint = f"http://{args.host}:{args.port}/mcp"
    logger.info("Starting mock compute-pool MCP server at {}", endpoint)
    logger.info("Lifecycle tools: submit_job, poll_job, collect_job, cancel_job")
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
