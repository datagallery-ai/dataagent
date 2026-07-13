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
"""Unit tests for MCP resource execution and list_resources."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dataagent.actions.tools.local_tool.sandbox import NoopSandbox
from dataagent.core.jobs.file_store import FileJobStore
from dataagent.core.jobs.service import JobService
from dataagent.core.resource_runtime import (
    ResourceJobCoordinator,
    build_default_operation_registry,
    default_mcp_client_factory,
)
from dataagent.resources import ResourceCapacity, ResourceCatalog, ResourceResolve


def _mcp_resources_config() -> dict[str, Any]:
    """Return a minimal MCP executable resource config."""
    return {
        "RESOURCES": [
            {
                "id": "compute_pool",
                "category": "executable",
                "transport": {
                    "type": "mcp",
                    "url": "https://compute.example.com/mcp",
                    "headers": {"Authorization": "token"},
                },
                "operations": {
                    "submit": "submit_job",
                    "poll": "poll_job",
                    "collect": "collect_job",
                    "cancel": "cancel_job",
                },
                "capacity": {"total": 2, "unit": "slot"},
                "consumption": {"*": 1},
            },
            {
                "id": "solver_license",
                "category": "non-executable",
                "capacity": {"total": 10, "unit": "license"},
                "consumption": {"optimization": 1},
            },
        ]
    }


def _build_mcp_service(tmp_path: Path) -> ResourceJobCoordinator:
    """Create ResourceJobCoordinator with MCP and catalog resources."""
    parent_ws = tmp_path / "parent_session"
    parent_ws.mkdir(parents=True, exist_ok=True)
    store = FileJobStore(parent_ws)
    job_service = JobService(store)
    runtime = SimpleNamespace(
        workspace_dir=parent_ws,
        sandbox=NoopSandbox(workspace_root=parent_ws),
    )
    catalog = ResourceCatalog.from_config(_mcp_resources_config())
    capacity = ResourceCapacity(catalog)
    resolve = ResourceResolve(catalog)
    return ResourceJobCoordinator(
        catalog=catalog,
        capacity=capacity,
        resolve=resolve,
        job_service=job_service,
        runtime=runtime,
        operation_registry=build_default_operation_registry(),
        mcp_client_factory=default_mcp_client_factory(),
    )


def _wait_until_terminal(service: JobService, job_id: str, *, timeout_sec: float = 5.0) -> str:
    """Poll until the job reaches a terminal status or timeout."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        snap = service.poll(job_id)
        if snap.status in {"completed", "failed", "cancelled", "timed_out"}:
            return snap.status
        time.sleep(0.05)
    return service.poll(job_id).status


def test_list_resources_returns_catalog_with_usage(tmp_path):
    """list_resources returns executable and non-executable entries."""
    service = _build_mcp_service(tmp_path)
    payload = service.list_resources()
    assert payload["status"] == "OK"
    assert len(payload["resources"]) == 2
    by_id = {item["id"]: item for item in payload["resources"]}
    assert by_id["compute_pool"]["category"] == "executable"
    assert by_id["compute_pool"]["available"] == 2
    assert by_id["solver_license"]["category"] == "non-executable"
    assert by_id["solver_license"]["capacity"]["unit"] == "license"


def test_mcp_resource_submit_poll_collect_with_mocked_driver(tmp_path, monkeypatch):
    """MCP resource jobs use the driver submit/poll/collect loop."""
    service = _build_mcp_service(tmp_path)
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_call(_client: Any, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(("mcp", tool_name, arguments))
        if tool_name == "submit_job":
            return {"status": "running", "job_id": "remote-42"}
        if tool_name == "poll_job":
            return {"status": "completed", "job_id": "remote-42"}
        if tool_name == "collect_job":
            return {"status": "completed", "summary": "remote done", "exit_code": 0}
        return {"status": "completed"}

    class FakeMcpClient:
        """Test double that records MCP tool invocations."""

        def call_tool_sync(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return fake_call(None, tool_name, arguments)

    monkeypatch.setattr(
        service,
        "get_mcp_client",
        lambda _resource_id, _driver: FakeMcpClient(),
    )

    handle = service.submit_job(command="python run.py", task_type="resource", resource_id="compute_pool")
    assert handle["status"] == "queued"
    job_id = handle["job_id"]
    assert _wait_until_terminal(service.job_service, job_id) == "completed"
    collected = service.collect(job_id=job_id)
    assert collected["status"] == "completed"
    assert "remote done" in str(collected.get("summary") or "")
    tool_names = [item[1] for item in calls]
    assert tool_names[0] == "submit_job"
    assert "poll_job" in tool_names
    assert "collect_job" in tool_names
    submit_args = calls[0][2]
    assert "envelope" in submit_args
    assert "allocation" in submit_args


def test_mcp_resource_failed_poll_collects_error_details(tmp_path, monkeypatch):
    """Failed MCP polls still invoke collect so ClickHouse errors surface to collect_job."""
    service = _build_mcp_service(tmp_path)
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_call(_client: Any, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append(("mcp", tool_name, arguments))
        if tool_name == "submit_job":
            return {"status": "running", "job_id": "remote-fail"}
        if tool_name == "poll_job":
            return {"status": "failed", "job_id": "remote-fail"}
        if tool_name == "collect_job":
            return {
                "status": "failed",
                "job_id": "remote-fail",
                "summary": "maximum sleep time is 3000000 microseconds",
                "error": "maximum sleep time is 3000000 microseconds",
                "exit_code": 1,
            }
        return {"status": "completed"}

    class FakeMcpClient:
        """Test double that records MCP tool invocations."""

        def call_tool_sync(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return fake_call(None, tool_name, arguments)

    monkeypatch.setattr(
        service,
        "get_mcp_client",
        lambda _resource_id, _driver: FakeMcpClient(),
    )

    handle = service.submit_job(command="SELECT sleep(10)", task_type="resource", resource_id="compute_pool")
    job_id = handle["job_id"]
    assert _wait_until_terminal(service.job_service, job_id) == "failed"
    collected = service.collect(job_id=job_id)
    assert collected["status"] == "failed"
    assert "maximum sleep time is 3000000 microseconds" in str(collected.get("error") or "")
    assert [item[1] for item in calls].count("collect_job") == 1
