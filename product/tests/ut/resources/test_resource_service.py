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
"""Unit and integration tests for ResourceJobCoordinator."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest

from dataagent.actions.tools.local_tool.sandbox import NoopSandbox
from dataagent.core.jobs.file_store import FileJobStore
from dataagent.core.jobs.models import JobResult
from dataagent.core.jobs.service import JobService
from dataagent.core.resource_runtime import (
    ResourceJobCoordinator,
    build_default_operation_registry,
    default_mcp_client_factory,
)
from dataagent.core.resource_runtime.operations.operations import ResourceOperationRegistry
from dataagent.resources import ResourceCapacity, ResourceCatalog, ResourceResolve


def _resources_config(*, total: int = 2) -> dict[str, Any]:
    """Return a minimal RESOURCES config for tests."""
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
                "capacity": {"total": total, "unit": "slot"},
                "consumption": {"*": 1, "batch_task": 2},
            }
        ]
    }


def _build_resource_coordinator(tmp_path: Path, *, total: int = 2) -> tuple[ResourceJobCoordinator, SimpleNamespace]:
    """Create ResourceJobCoordinator bound to a parent workspace."""
    parent_ws = tmp_path / "parent_session"
    parent_ws.mkdir(parents=True, exist_ok=True)
    store = FileJobStore(parent_ws)
    job_service = JobService(store)
    runtime = SimpleNamespace(
        workspace_dir=parent_ws,
        sandbox=NoopSandbox(workspace_root=parent_ws),
    )
    catalog = ResourceCatalog.from_config(_resources_config(total=total))
    capacity = ResourceCapacity(catalog)
    resolve = ResourceResolve(catalog)
    coordinator = ResourceJobCoordinator(
        catalog=catalog,
        capacity=capacity,
        resolve=resolve,
        job_service=job_service,
        runtime=runtime,
        operation_registry=build_default_operation_registry(),
        mcp_client_factory=default_mcp_client_factory(),
    )
    return coordinator, runtime


def _build_resource_service(tmp_path: Path, *, total: int = 2) -> tuple[ResourceJobCoordinator, SimpleNamespace]:
    """Backward-compatible alias used by tests in this module."""
    return _build_resource_coordinator(tmp_path, total=total)


def _wait_until_terminal(service: JobService, job_id: str, *, timeout_sec: float = 5.0) -> str:
    """Poll until the job reaches a terminal status or timeout."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        snap = service.poll(job_id)
        if snap.status in {"completed", "failed", "cancelled", "timed_out"}:
            return snap.status
        time.sleep(0.05)
    return service.poll(job_id).status


def test_submit_job_registers_runner_and_metadata(tmp_path):
    """submit_job queues a job with resource metadata and allocation."""
    service, _runtime = _build_resource_service(tmp_path)
    captured: dict[str, Any] = {}

    original_start = service.job_service.start

    def spy_start(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return original_start(**kwargs)

    service.job_service.start = spy_start  # type: ignore[method-assign]
    payload = service.submit_job(command="echo hello", task_type="resource")
    assert payload["status"] == "queued"
    assert payload["resource_id"] == "local"
    assert captured["metadata"]["job_kind"] == "resource"
    assert captured["metadata"]["resource_id"] == "local"
    assert captured["allocation"]["resource"]["amount"] == 1
    job_id = payload["job_id"]
    assert _wait_until_terminal(service.job_service, job_id) == "completed"


def test_capacity_exhausted_returns_error(tmp_path):
    """Active reserved slots block further submits."""
    service, _runtime = _build_resource_service(tmp_path, total=2)
    first = service.submit_job(command="sleep 5", task_type="resource", resource_id="local")
    second = service.submit_job(command="sleep 5", task_type="resource", resource_id="local")
    assert first["status"] == "queued"
    assert second["status"] == "queued"
    denied = service.submit_job(command="echo blocked", task_type="resource", resource_id="local")
    assert denied["status"] == "ERROR"
    assert "capacity exhausted" in denied["message"]


def test_submit_job_rejects_when_capacity_full(tmp_path):
    """submit_job enforces capacity while holding the allocation lock."""
    service, _runtime = _build_resource_service(tmp_path, total=2)
    first = service.submit_job(command="sleep 5", task_type="resource", resource_id="local")
    second = service.submit_job(command="sleep 5", task_type="resource", resource_id="local")
    denied = service.submit_job(command="sleep 5", task_type="resource", resource_id="local")
    assert first["status"] == "queued"
    assert second["status"] == "queued"
    assert denied["status"] == "ERROR"
    assert "capacity exhausted" in denied["message"]


def test_concurrent_submit_does_not_oversell_capacity(tmp_path):
    """Concurrent submits respect pending + active usage when capacity.total=1."""
    service, _runtime = _build_resource_service(tmp_path, total=1)
    barrier = threading.Barrier(2)
    results: list[dict[str, Any]] = []
    results_lock = threading.Lock()

    def submit_once() -> None:
        barrier.wait(timeout=5.0)
        payload = service.submit_job(
            command="sleep 2",
            task_type="resource",
            resource_id="local",
            timeout_sec=30,
        )
        with results_lock:
            results.append(payload)

    threads = [threading.Thread(target=submit_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
        assert not thread.is_alive()

    assert len(results) == 2
    queued = [item for item in results if item.get("status") == "queued"]
    denied = [item for item in results if item.get("status") == "ERROR"]
    assert len(queued) == 1
    assert len(denied) == 1
    assert "capacity exhausted" in str(denied[0].get("message") or "")

    for handle in queued:
        assert _wait_until_terminal(service.job_service, str(handle["job_id"])) == "completed"


def test_submit_job_rejects_fifth_when_capacity_total_is_four(tmp_path):
    """Serial submits exhaust capacity.total=4; the fifth submit returns ERROR."""
    service, _runtime = _build_resource_service(tmp_path, total=4)
    for index in range(4):
        handle = service.submit_job(
            command="sleep 3",
            task_type="resource",
            resource_id="local",
            timeout_sec=60,
        )
        assert handle["status"] == "queued", index

    catalog = service.list_resources()
    local_entry = next(item for item in catalog["resources"] if item["id"] == "local")
    assert local_entry["used"] == 4
    assert local_entry["available"] == 0

    denied = service.submit_job(
        command="sleep 3",
        task_type="resource",
        resource_id="local",
        timeout_sec=60,
    )
    assert denied["status"] == "ERROR"
    assert "capacity exhausted" in str(denied.get("message") or "")
    assert "/4 slot" in str(denied.get("message") or "")


def test_concurrent_submit_rejects_fifth_when_capacity_total_is_four(tmp_path):
    """Five concurrent submits respect capacity.total=4 (four queued, one ERROR)."""
    service, _runtime = _build_resource_service(tmp_path, total=4)
    barrier = threading.Barrier(5)
    results: list[dict[str, Any]] = []
    results_lock = threading.Lock()

    def submit_once() -> None:
        barrier.wait(timeout=5.0)
        payload = service.submit_job(
            command="sleep 3",
            task_type="resource",
            resource_id="local",
            timeout_sec=60,
        )
        with results_lock:
            results.append(payload)

    threads = [threading.Thread(target=submit_once) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15.0)
        assert not thread.is_alive()

    assert len(results) == 5
    queued = [item for item in results if item.get("status") == "queued"]
    denied = [item for item in results if item.get("status") == "ERROR"]
    assert len(queued) == 4
    assert len(denied) == 1
    assert "capacity exhausted" in str(denied[0].get("message") or "")
    assert "/4 slot" in str(denied[0].get("message") or "")

    catalog = service.list_resources()
    local_entry = next(item for item in catalog["resources"] if item["id"] == "local")
    assert local_entry["used"] == 4
    assert local_entry["available"] == 0


def test_pending_released_on_command_validation_failure(tmp_path):
    """Pending capacity is released when command validation fails."""
    service, _runtime = _build_resource_service(tmp_path)
    first = service.submit_job(command="echo ok", task_type="resource")
    assert first["status"] == "queued"
    denied = service.submit_job(command="", task_type="resource")
    assert denied["status"] == "ERROR"
    second = service.submit_job(command="echo ok2", task_type="resource")
    assert second["status"] == "queued"


def test_unknown_operation_raises(tmp_path):
    """ResourceOperationRegistry rejects unknown operation ids."""
    service, _runtime = _build_resource_service(tmp_path)
    registry = ResourceOperationRegistry()
    service.operation_registry = registry
    payload = service.submit_job(command="echo hello", task_type="resource")
    assert payload["status"] == "queued"
    assert _wait_until_terminal(service.job_service, payload["job_id"]) == "failed"


def test_integration_echo_command(tmp_path):
    """End-to-end local sandbox command writes a completed collect result."""
    service, _runtime = _build_resource_service(tmp_path, total=4)
    handle = service.submit_job(command="echo hello", task_type="resource")
    job_id = handle["job_id"]
    assert _wait_until_terminal(service.job_service, job_id) == "completed"
    collected = service.collect(job_id=job_id)
    assert collected["status"] == "completed"
    assert "hello" in str(collected.get("summary") or "")


def test_consumption_by_batch_task_occupies_two_slots(tmp_path):
    """batch_task consumption of 2 exhausts capacity.total=2 with one active job."""
    service, _runtime = _build_resource_service(tmp_path, total=2)
    handle = service.submit_job(command="sleep 0.2", task_type="batch_task")
    assert handle["status"] == "queued"
    assert handle["allocation"]["amount"] == 2
    denied = service.submit_job(command="echo blocked", task_type="resource", resource_id="local")
    assert denied["status"] == "ERROR"
    assert _wait_until_terminal(service.job_service, handle["job_id"]) == "completed"
    retry = service.submit_job(command="echo ok", task_type="resource", resource_id="local")
    assert retry["status"] == "queued"


def test_cancel_job_terminates_running_resource_job(tmp_path):
    """cancel on a blocking resource job persists cancelled terminal state."""
    service, _runtime = _build_resource_service(tmp_path, total=4)
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

    service.job_service.start(
        agent_id="resource:local",
        task="block",
        runner=slow_runner,
        metadata={"job_kind": "resource", "resource_id": "local"},
        allocation={"resource": {"id": "local", "task_type": "resource", "amount": 1, "unit": "slot"}},
    )
    statuses = service.job_service.store.list_statuses()
    job_id = str(statuses[-1]["job_id"])
    assert started.wait(timeout=2.0)
    cancel_snap = service.cancel(job_id=job_id)
    assert cancel_snap["status"] == "cancelled"
    assert _wait_until_terminal(service.job_service, job_id) == "cancelled"


def test_script_artifact_must_live_inside_workspace(tmp_path):
    """script_artifact outside workspace is rejected before job creation."""
    service, runtime = _build_resource_service(tmp_path)
    outside = tmp_path / "outside.sh"
    outside.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    denied = service.submit_job(
        command="echo noop",
        task_type="resource",
        script_artifact={"path": str(outside)},
    )
    assert denied["status"] == "ERROR"
    assert "script_artifact" in denied["message"]

    script = runtime.workspace_dir / "run.sh"
    script.write_text("#!/bin/sh\necho from_script\n", encoding="utf-8")
    handle = service.submit_job(
        command="sh run.sh",
        task_type="resource",
        script_artifact={"path": "run.sh"},
    )
    assert handle["status"] == "queued"
    assert _wait_until_terminal(service.job_service, handle["job_id"]) == "completed"
    collected = service.collect(job_id=handle["job_id"])
    assert "from_script" in str(collected.get("summary") or "")


def test_start_failure_returns_error_instead_of_raising(tmp_path, monkeypatch):
    """JobService.start failures surface as ERROR dicts for tool consumers."""
    service, _runtime = _build_resource_service(tmp_path, total=4)

    def boom(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("start failed")

    monkeypatch.setattr(service.job_service, "start", boom)
    denied = service.submit_job(command="echo hello", task_type="resource")
    assert denied["status"] == "ERROR"
    assert "start failed" in denied["message"]
    monkeypatch.undo()
    retry = service.submit_job(command="echo hello", task_type="resource")
    assert retry["status"] == "queued"


def test_collect_result_uses_shared_job_result_schema(tmp_path):
    """Resource jobs persist Galatea-style summary/metrics without subagent business fields."""
    service, _runtime = _build_resource_service(tmp_path, total=4)
    handle = service.submit_job(command="echo payload", task_type="resource")
    job_id = handle["job_id"]
    assert _wait_until_terminal(service.job_service, job_id) == "completed"
    collected = service.collect(job_id=job_id)
    assert collected["summary"] == "payload"
    assert collected["metrics"]["exit_code"] == 0
    assert collected.get("subagent_session_id") == ""
    assert collected.get("workspace_rel_path") == ""
    assert collected.get("original_msg") is None
