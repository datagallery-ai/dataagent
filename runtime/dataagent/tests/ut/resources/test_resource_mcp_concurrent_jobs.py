# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# ============================================================================
"""Concurrent MCP resource jobs through ResourceJobCoordinator (no LLM).

FVT resource_loader_077 asks the LLM to submit N jobs. This test hits the
same coordinator + cached MCP client path with JOB_COUNT=20, so a client
race fails here without depending on the model.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dataagent.actions.tools.local_tool.sandbox import NoopSandbox
from dataagent.core.jobs.file_store import FileJobStore
from dataagent.core.jobs.models import TERMINAL_STATUSES
from dataagent.core.jobs.service import JobService
from dataagent.core.resource_runtime import (
    ResourceJobCoordinator,
    build_default_operation_registry,
    default_mcp_client_factory,
)
from dataagent.resources import ResourceCapacity, ResourceCatalog, ResourceResolve

JOB_COUNT = 20
_COMMANDS = [
    "echo alpha",
    "echo bravo",
    "echo charlie",
    "echo delta",
    "echo echo",
    "echo foxtrot",
    "echo golf",
    "echo hotel",
    "echo india",
    "echo juliet",
    "echo kilo",
    "echo lima",
    "echo mike",
    "echo november",
    "echo oscar",
    "echo papa",
    "echo quebec",
    "echo romeo",
    "echo sierra",
    "echo tango",
]


def _free_port() -> int:
    """Return an unused TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_mock(url: str, *, timeout_sec: float = 20.0) -> None:
    """Block until the mock MCP initialize handshake succeeds."""
    deadline = time.monotonic() + timeout_sec
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "ut", "version": "0.1.0"},
            },
        }
    ).encode()
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.2)
    raise RuntimeError(f"mock MCP did not become ready: {url}")


@pytest.fixture()
def mock_mcp_url() -> Iterator[str]:
    """Start mock_compute_pool and yield its streamable HTTP URL."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "dataagent.actions.tools.mcp_tool.mock_compute_pool",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_mock(url)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _resources_config(mcp_url: str) -> dict[str, Any]:
    """Return RESOURCES config matching the FVT mock_compute_pool entry."""
    return {
        "RESOURCES": [
            {
                "id": "mock_compute_pool",
                "name": "Mock Compute Pool",
                "category": "executable",
                "transport": {"type": "mcp", "url": mcp_url},
                "operations": {
                    "submit": "submit_job",
                    "poll": "poll_job",
                    "collect": "collect_job",
                    "cancel": "cancel_job",
                },
                "capacity": {"total": 20, "unit": "slot"},
                "consumption": {"*": 1},
            }
        ]
    }


def _build_coordinator(tmp_path: Path, mcp_url: str) -> ResourceJobCoordinator:
    """Build a real MCP coordinator against the live mock server."""
    parent_ws = tmp_path / "parent_session"
    parent_ws.mkdir(parents=True, exist_ok=True)
    runtime = SimpleNamespace(
        workspace_dir=parent_ws,
        sandbox=NoopSandbox(workspace_root=parent_ws),
    )
    catalog = ResourceCatalog.from_config(_resources_config(mcp_url))
    return ResourceJobCoordinator(
        catalog=catalog,
        capacity=ResourceCapacity(catalog),
        resolve=ResourceResolve(catalog),
        job_service=JobService(FileJobStore(parent_ws)),
        runtime=runtime,
        operation_registry=build_default_operation_registry(),
        mcp_client_factory=default_mcp_client_factory(),
    )


def _wait_until_terminal(service: JobService, job_id: str, *, timeout_sec: float) -> str:
    """Poll one job until it reaches a terminal status or timeout."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        status = service.poll(job_id).status
        if status in TERMINAL_STATUSES:
            return status
        time.sleep(0.1)
    return service.poll(job_id).status


def test_twenty_concurrent_mcp_resource_jobs_complete(tmp_path: Path, mock_mcp_url: str) -> None:
    """Twenty concurrent coordinator submits must all complete and release slots.

    This is the DataAgent-side equivalent of FVT resource_loader_077 (JOB_COUNT=20),
    without going through the LLM.
    """
    coordinator = _build_coordinator(tmp_path, mock_mcp_url)

    def _submit(index: int) -> dict[str, Any]:
        """Submit from a thread that already has a running event loop.

        DataAgent's MCP helper then nests ``asyncio.run`` in another thread.
        Direct mock clients do not take this path.
        """

        async def _submit_with_running_loop() -> dict[str, Any]:
            return coordinator.submit_job(
                command=_COMMANDS[index],
                task_type="sandbox",
                resource_id="mock_compute_pool",
                timeout_sec=120,
            )

        return asyncio.run(_submit_with_running_loop())

    with ThreadPoolExecutor(max_workers=JOB_COUNT) as pool:
        futures = [pool.submit(_submit, index) for index in range(JOB_COUNT)]
        handles = [future.result() for future in as_completed(futures)]

    assert all(item.get("status") == "queued" for item in handles), handles
    job_ids = [str(item["job_id"]) for item in handles]
    assert len(job_ids) == JOB_COUNT

    with ThreadPoolExecutor(max_workers=JOB_COUNT) as pool:
        wait_futures = [
            pool.submit(_wait_until_terminal, coordinator.job_service, job_id, timeout_sec=90.0) for job_id in job_ids
        ]
        statuses = [future.result() for future in wait_futures]
    assert statuses == ["completed"] * JOB_COUNT, statuses

    collected = [coordinator.collect(job_id=job_id) for job_id in job_ids]
    assert all(item.get("status") == "completed" for item in collected), collected

    usage = {item["id"]: item for item in coordinator.list_resources()["resources"]}
    assert usage["mock_compute_pool"]["used"] == 0
    assert usage["mock_compute_pool"]["available"] == 20
