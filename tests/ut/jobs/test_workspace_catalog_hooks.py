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
"""AgentService hooks that persist workspace_catalog.json."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from dataagent.agents.galatea.utils.json_store import read_json_object
from dataagent.core.agents.registry import AgentRegistry
from dataagent.core.agents.service import AgentService
from dataagent.core.jobs.file_store import FileJobStore
from dataagent.core.jobs.models import JobResult
from dataagent.core.jobs.service import JobService
from dataagent.core.workspace.catalog import catalog_path


def _wait_until_terminal(service: JobService, job_id: str, *, timeout_sec: float = 5.0) -> str:
    """Poll until job reaches a terminal status or timeout."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        snap = service.poll(job_id)
        if snap.status in {"completed", "failed", "cancelled", "timed_out"}:
            return snap.status
        time.sleep(0.05)
    return service.poll(job_id).status


def _build_agent_service(tmp_path: Path, *, runner_factory) -> tuple[AgentService, SimpleNamespace]:
    """Minimal AgentService + runtime for catalog hook tests."""
    parent_ws = tmp_path / "parent_session"
    parent_ws.mkdir(parents=True, exist_ok=True)
    store = FileJobStore(parent_ws)
    job_service = JobService(store)
    runtime = SimpleNamespace(
        workspace_dir=parent_ws,
        session_id="parent_sess_abc",
        user_id="test_user",
        sandbox=SimpleNamespace(wrap=lambda cmd, **kwargs: cmd),
        on_subagent_progress=None,
        env=SimpleNamespace(config_manager=SimpleNamespace(get=lambda *_args, **_kwargs: 4)),
        get_all_config=lambda: {},
    )

    subagent_yaml = tmp_path / "arith.yaml"
    subagent_yaml.write_text(
        yaml.safe_dump({"AGENT_CONFIG": {"id": "arith", "name": "arith", "description": "math"}}),
        encoding="utf-8",
    )
    registry = AgentRegistry.from_subagent_configs([{"path": str(subagent_yaml)}])

    class _Adapter:
        def run(self, **kwargs: Any) -> JobResult:
            return runner_factory(**kwargs)

    service = AgentService(registry=registry, job_service=job_service, runtime=runtime, adapter=_Adapter())
    return service, runtime


def _catalog_payload(workspace_root: Path) -> dict[str, Any]:
    """Load raw workspace_catalog.json from disk."""
    return read_json_object(catalog_path(workspace_root), {})


def test_submit_new_workspace_writes_catalog_entry(tmp_path: Path) -> None:
    """Submit on a new workspace registers one catalog entry and job."""

    def runner(**kwargs: Any) -> JobResult:
        return JobResult(
            job_id=kwargs["job_id"],
            agent_id="arith",
            status="completed",
            summary="ok",
            original_msg={"final_answer": "2"},
            frontend_msg="2",
            state={"done": True},
            subagent_session_id=kwargs["subagent_session_id"],
            workspace_rel_path=kwargs["workspace_rel_path"],
        )

    service, runtime = _build_agent_service(tmp_path, runner_factory=runner)
    payload = service.submit(agent_id="arith", task="1+1")
    assert payload["reused_workspace"] is False
    catalog = _catalog_payload(runtime.workspace_dir)
    assert len(catalog.get("subagent_workspace", {})) == 1
    entry = next(iter(catalog["subagent_workspace"].values()))
    assert len(entry["jobs"]) == 1


def test_submit_reuse_appends_job_without_new_entry(tmp_path: Path) -> None:
    """Reused workspace keeps one entry and appends a second job."""

    def runner(**kwargs: Any) -> JobResult:
        return JobResult(
            job_id=kwargs["job_id"],
            agent_id="arith",
            status="completed",
            summary="ok",
            original_msg={"final_answer": "2"},
            frontend_msg="2",
            state={"done": True},
            subagent_session_id=kwargs["subagent_session_id"],
            workspace_rel_path=kwargs["workspace_rel_path"],
        )

    service, runtime = _build_agent_service(tmp_path, runner_factory=runner)
    first = service.submit(agent_id="arith", task="first")
    assert _wait_until_terminal(service.job_service, first["job_id"]) == "completed"
    second = service.submit(
        agent_id="arith",
        task="second",
        job_envelope={"workspace_rel_path": first["workspace_rel_path"]},
    )
    assert second["reused_workspace"] is True
    catalog = _catalog_payload(runtime.workspace_dir)
    assert len(catalog["subagent_workspace"]) == 1
    entry = next(iter(catalog["subagent_workspace"].values()))
    assert len(entry["jobs"]) == 2


def test_poll_completed_refreshes_artifacts_without_collect(tmp_path: Path) -> None:
    """Poll on completed job refreshes catalog artifacts without collect."""

    def runner(**kwargs: Any) -> JobResult:
        (kwargs["workspace_dir"] / "result.md").write_text("done", encoding="utf-8")
        return JobResult(
            job_id=kwargs["job_id"],
            agent_id="arith",
            status="completed",
            summary="ok",
            original_msg={"final_answer": "2"},
            frontend_msg="2",
            state={"done": True},
            subagent_session_id=kwargs["subagent_session_id"],
            workspace_rel_path=kwargs["workspace_rel_path"],
        )

    service, runtime = _build_agent_service(tmp_path, runner_factory=runner)
    handle = service.submit(agent_id="arith", task="write")
    job_id = handle["job_id"]
    assert _wait_until_terminal(service.job_service, job_id) == "completed"
    polled = service.poll(job_id=job_id)
    assert polled["status"] == "completed"
    catalog = _catalog_payload(runtime.workspace_dir)
    entry = catalog["subagent_workspace"][handle["subagent_session_id"]]
    assert "result.md" in entry["artifacts"]


def test_poll_failed_does_not_refresh_artifacts(tmp_path: Path) -> None:
    """Failed job poll must not populate catalog artifacts."""

    def runner(**kwargs: Any) -> JobResult:
        (kwargs["workspace_dir"] / "partial.md").write_text("x", encoding="utf-8")
        return JobResult(
            job_id=kwargs["job_id"],
            agent_id="arith",
            status="failed",
            summary="boom",
            original_msg={"error": "boom"},
            frontend_msg="boom",
            state={"done": False},
            subagent_session_id=kwargs["subagent_session_id"],
            workspace_rel_path=kwargs["workspace_rel_path"],
        )

    service, runtime = _build_agent_service(tmp_path, runner_factory=runner)
    handle = service.submit(agent_id="arith", task="fail")
    job_id = handle["job_id"]
    assert _wait_until_terminal(service.job_service, job_id) == "failed"
    service.poll(job_id=job_id)
    catalog = _catalog_payload(runtime.workspace_dir)
    entry = catalog["subagent_workspace"][handle["subagent_session_id"]]
    assert entry["artifacts"] == []


def test_collect_completed_refreshes_artifacts(tmp_path: Path) -> None:
    """Collect on completed job refreshes catalog artifacts."""
    captured: dict[str, Any] = {}

    def runner(**kwargs: Any) -> JobResult:
        captured["workspace_dir"] = kwargs["workspace_dir"]
        (kwargs["workspace_dir"] / "result.md").write_text("done", encoding="utf-8")
        return JobResult(
            job_id=kwargs["job_id"],
            agent_id="arith",
            status="completed",
            summary="ok",
            original_msg={"final_answer": "2"},
            frontend_msg="2",
            state={"done": True},
            subagent_session_id=kwargs["subagent_session_id"],
            workspace_rel_path=kwargs["workspace_rel_path"],
        )

    service, runtime = _build_agent_service(tmp_path, runner_factory=runner)
    handle = service.submit(agent_id="arith", task="write")
    job_id = handle["job_id"]
    assert _wait_until_terminal(service.job_service, job_id) == "completed"
    collected = service.collect(job_id=job_id)
    assert collected["status"] == "completed"
    catalog = _catalog_payload(runtime.workspace_dir)
    entry = catalog["subagent_workspace"][handle["subagent_session_id"]]
    assert "result.md" in entry["artifacts"]


def test_collect_failed_does_not_refresh_artifacts(tmp_path: Path) -> None:
    """Collect on failed job leaves catalog artifacts empty."""

    def runner(**kwargs: Any) -> JobResult:
        (kwargs["workspace_dir"] / "partial.md").write_text("x", encoding="utf-8")
        return JobResult(
            job_id=kwargs["job_id"],
            agent_id="arith",
            status="failed",
            summary="boom",
            original_msg={"error": "boom"},
            frontend_msg="boom",
            state={"done": False},
            subagent_session_id=kwargs["subagent_session_id"],
            workspace_rel_path=kwargs["workspace_rel_path"],
        )

    service, runtime = _build_agent_service(tmp_path, runner_factory=runner)
    handle = service.submit(agent_id="arith", task="fail")
    job_id = handle["job_id"]
    assert _wait_until_terminal(service.job_service, job_id) == "failed"
    service.collect(job_id=job_id)
    catalog = _catalog_payload(runtime.workspace_dir)
    entry = catalog["subagent_workspace"][handle["subagent_session_id"]]
    assert entry["artifacts"] == []
    assert len(entry["jobs"]) == 1


def test_catalog_write_failure_does_not_break_submit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Catalog persistence errors must not block job submit."""

    def runner(**kwargs: Any) -> JobResult:
        return JobResult(
            job_id=kwargs["job_id"],
            agent_id="arith",
            status="completed",
            summary="ok",
            original_msg={"final_answer": "2"},
            frontend_msg="2",
            state={"done": True},
            subagent_session_id=kwargs["subagent_session_id"],
            workspace_rel_path=kwargs["workspace_rel_path"],
        )

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("dataagent.core.workspace.catalog.save_catalog", _boom)
    service, _runtime = _build_agent_service(tmp_path, runner_factory=runner)
    payload = service.submit(agent_id="arith", task="still works")
    assert payload["status"] == "queued"
