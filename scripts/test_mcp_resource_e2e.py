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
"""Manual E2E script for MCP executable resources against the mock compute pool."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dataagent.actions.resources.bootstrap import (
    default_mcp_client_factory,
    default_resource_operation_registry,
)
from dataagent.actions.tools.local_tool.sandbox import NoopSandbox
from dataagent.core.jobs.file_store import FileJobStore
from dataagent.core.jobs.service import JobService
from dataagent.core.resources.registry import ResourceRegistry
from dataagent.core.resources.service import ResourceService


def _resources_config(*, mcp_url: str) -> dict:
    """Build RESOURCES pointing at the mock compute pool."""
    return {
        "RESOURCES": [
            {
                "id": "mock_compute_pool",
                "name": "Mock Compute Pool",
                "category": "executable",
                "transport": {
                    "type": "mcp",
                    "url": mcp_url,
                },
                "operations": {
                    "submit": "submit_job",
                    "poll": "poll_job",
                    "collect": "collect_job",
                    "cancel": "cancel_job",
                },
                "capacity": {"total": 4, "unit": "slot"},
                "consumption": {"*": 1},
            }
        ]
    }


def _wait_terminal(job_service: JobService, job_id: str, *, timeout_sec: float = 30.0) -> str:
    """Poll until one job reaches a terminal status."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        snap = job_service.poll(job_id)
        if snap.status in {"completed", "failed", "cancelled", "timed_out"}:
            return snap.status
        time.sleep(0.2)
    return job_service.poll(job_id).status


def main() -> int:
    """Run one submit → poll → collect cycle against the mock MCP pool."""
    parser = argparse.ArgumentParser(description="Test Ferry MCP resource against mock compute pool.")
    parser.add_argument(
        "--mcp-url",
        default="http://127.0.0.1:8765/mcp",
        help="Mock compute pool streamable HTTP endpoint",
    )
    parser.add_argument("--workspace", default="", help="Optional parent workspace directory")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser() if args.workspace else Path.cwd() / ".mcp_resource_test_ws"
    workspace.mkdir(parents=True, exist_ok=True)

    store = FileJobStore(workspace)
    job_service = JobService(store)
    runtime = SimpleNamespace(workspace_dir=workspace, sandbox=NoopSandbox(workspace_root=workspace))
    registry = ResourceRegistry.from_config(_resources_config(mcp_url=args.mcp_url))
    service = ResourceService(
        registry=registry,
        job_service=job_service,
        runtime=runtime,
        operation_registry=default_resource_operation_registry(),
        mcp_client_factory=default_mcp_client_factory(),
    )

    print(f"workspace: {workspace}")
    print(f"mcp url:   {args.mcp_url}")
    print("submit_resource_job ...")
    handle = service.submit_job(
        command="echo hello-from-mcp-resource",
        task_type="resource",
        resource_id="mock_compute_pool",
    )
    if handle.get("status") != "queued":
        print("submit failed:", handle)
        return 1
    job_id = str(handle["job_id"])
    print(f"queued job_id={job_id}")

    terminal = _wait_terminal(job_service, job_id)
    print(f"terminal status: {terminal}")
    collected = service.collect(job_id=job_id)
    print("collect:", collected)
    if terminal != "completed":
        return 1
    summary = str(collected.get("summary") or "")
    if "hello-from-mcp-resource" not in summary:
        print("unexpected summary:", summary)
        return 1
    print("OK: MCP resource E2E passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
