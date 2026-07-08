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
"""Phase A acceptance tests mapped to ferry_job子系统_PRD §11."""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.job_tools.poll_subagent import poll_subagent
from dataagent.actions.tools.local_tool.job_tools.submit_subagent import submit_subagent
from dataagent.core.agents.registry import AgentRegistry
from dataagent.core.agents.service import AgentService
from dataagent.core.agents.subagent_subprocess_runner import (
    JobSubagentOutcome,
    SubagentSubprocessRunner,
    _parse_job_subagent_completed,
)
from dataagent.core.jobs.file_store import FileJobStore
from dataagent.core.jobs.models import JobResult
from dataagent.core.jobs.service import JobService
from dataagent.core.managers.action_manager.manager import ToolManager


def _wait_until_terminal(service: JobService, job_id: str, *, timeout_sec: float = 5.0) -> str:
    """Poll until the job reaches a terminal status or timeout."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        snap = service.poll(job_id)
        if snap.status in {"completed", "failed", "cancelled", "timed_out"}:
            return snap.status
        time.sleep(0.05)
    return service.poll(job_id).status


def _build_agent_service(tmp_path: Path, *, runner_factory) -> tuple[AgentService, SimpleNamespace]:
    """Create AgentService with a parent workspace and custom job runner."""
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


def test_ac03_empty_subagent_configs_skips_job_tools():
    """AC-03: empty SUBAGENT_CONFIGS must not register lifecycle tools."""
    tm = ToolManager()
    tm._register_implicit_job_tools({"SUBAGENT_CONFIGS": [], "RESOURCES": [{"id": "gpu"}]})
    for name in (
        "submit_subagent",
        "poll_subagent",
        "collect_subagent",
        "cancel_subagent",
        "search_workspaces",
        "inspect_workspace",
    ):
        assert not tm.exists(name)


def test_ac05_submit_returns_job_metadata_and_jobs_dir(tmp_path):
    """AC-05: submit returns ids and persists under ``{parent_ws}/jobs/{job_id}/``."""

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
    assert payload["status"] == "queued"
    assert payload["job_id"]
    assert payload["agent_id"] == "arith"
    assert payload["subagent_session_id"]
    assert payload["workspace_rel_path"].startswith("subagents/")
    job_id = payload["job_id"]
    assert _wait_until_terminal(service.job_service, job_id) == "completed"
    assert (runtime.workspace_dir / "jobs" / job_id / "job.json").is_file()


def test_ac06_collect_before_terminal_returns_message_only(tmp_path):
    """AC-06: collect before terminal must not expose full §6.3 payload."""

    def runner(**kwargs: Any) -> JobResult:
        time.sleep(0.4)
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

    service, _runtime = _build_agent_service(tmp_path, runner_factory=runner)
    handle = service.submit(agent_id="arith", task="slow")
    job_id = handle["job_id"]
    time.sleep(0.05)
    early = service.collect(job_id=job_id)
    assert "message" in early
    assert "original_msg" not in early
    assert _wait_until_terminal(service.job_service, job_id) == "completed"


def test_ac06b_poll_watch_collects_snapshots_without_auto_collect(monkeypatch):
    """AC-06b: watch mode loops poll and does not call collect."""
    calls: list[str] = []

    class _AgentService:
        def poll(self, *, job_id: str, cursor: str | None = None, event_limit: int = 20) -> dict[str, Any]:
            calls.append("poll")
            if len(calls) == 1:
                return {"job_id": job_id, "status": "running", "cursor": "c1", "events": []}
            return {"job_id": job_id, "status": "completed", "cursor": "c2", "events": []}

        def collect(self, *, job_id: str) -> dict[str, Any]:
            calls.append("collect")
            return {"status": "completed"}

    runtime = SimpleNamespace(
        ensure_job_services=lambda: _AgentService(),
        ensure_not_cancelled=lambda: None,
    )
    ctx = ToolExecutionContext(runtime=runtime)
    monkeypatch.setattr(time, "sleep", lambda _sec: None)
    payload = poll_subagent(
        job_id="job-1",
        watch_sec=4,
        interval_sec=0.01,
        _tool_context=ctx,
    )
    assert payload["status"] == "completed"
    assert payload["watch"]["enabled"] is True
    assert len(payload["watch"]["snapshots"]) >= 2
    assert calls.count("poll") >= 2
    assert "collect" not in calls


def test_ac07_collect_completed_includes_business_fields_and_workspace(tmp_path):
    """AC-07: completed collect returns §6.3 fields plus workspace identifiers."""

    def runner(**kwargs: Any) -> JobResult:
        return JobResult(
            job_id=kwargs["job_id"],
            agent_id="arith",
            status="completed",
            summary="ok",
            original_msg={"final_answer": "42"},
            frontend_msg="42",
            state={"complete": True},
            subagent_session_id=kwargs["subagent_session_id"],
            workspace_rel_path=kwargs["workspace_rel_path"],
        )

    service, _runtime = _build_agent_service(tmp_path, runner_factory=runner)
    handle = service.submit(agent_id="arith", task="answer")
    job_id = handle["job_id"]
    assert _wait_until_terminal(service.job_service, job_id) == "completed"
    collected = service.collect(job_id=job_id)
    assert collected["status"] == "completed"
    assert collected["original_msg"] == {"final_answer": "42"}
    assert collected["frontend_msg"] == "42"
    assert collected["state"] == {"complete": True}
    assert collected["subagent_session_id"] == handle["subagent_session_id"]
    assert collected["workspace_rel_path"] == handle["workspace_rel_path"]
    assert "sub_id" not in collected


def test_ac07b_non_completed_collect_is_structured_without_fake_business_fields(tmp_path):
    """AC-07b: failed/cancelled/timed_out collect stays structured and honest."""

    def fail_runner(**kwargs: Any) -> JobResult:
        return JobResult(
            job_id=kwargs["job_id"],
            agent_id="arith",
            status="failed",
            summary="boom",
            error="boom",
            subagent_session_id=kwargs["subagent_session_id"],
            workspace_rel_path=kwargs["workspace_rel_path"],
        )

    fail_service, _ = _build_agent_service(tmp_path, runner_factory=fail_runner)
    fail_handle = fail_service.submit(agent_id="arith", task="fail")
    fail_id = fail_handle["job_id"]
    assert _wait_until_terminal(fail_service.job_service, fail_id) == "failed"
    failed = fail_service.collect(job_id=fail_id)
    assert failed["status"] == "failed"
    assert failed.get("error")
    assert "original_msg" not in failed or failed.get("original_msg") in (None, "")

    def slow_runner(**kwargs: Any) -> JobResult:
        time.sleep(2.0)
        return JobResult(job_id=kwargs["job_id"], agent_id="arith", status="completed", summary="late")

    slow_service, _ = _build_agent_service(tmp_path / "timeout_case", runner_factory=slow_runner)
    slow_handle = slow_service.submit(agent_id="arith", task="slow", timeout_sec=1)
    slow_id = slow_handle["job_id"]
    assert _wait_until_terminal(slow_service.job_service, slow_id, timeout_sec=4) == "timed_out"
    timed_out = slow_service.collect(job_id=slow_id)
    assert timed_out["status"] == "timed_out"
    assert timed_out["subagent_session_id"] == slow_handle["subagent_session_id"]
    assert timed_out["workspace_rel_path"] == slow_handle["workspace_rel_path"]
    assert timed_out.get("original_msg") in (None, "")


def test_ac08_cancel_running_job(tmp_path):
    """AC-08: cancel transitions running job to cancelled."""
    store = FileJobStore(tmp_path)
    service = JobService(store)
    started = __import__("threading").Event()

    def runner(job_id: str, cancel_event) -> JobResult:
        started.set()
        while not cancel_event.is_set():
            time.sleep(0.05)
        return JobResult(
            job_id=job_id,
            agent_id="demo",
            status="cancelled",
            summary="Agent job cancelled.",
            subagent_session_id="sess-cancel",
            workspace_rel_path="subagents/cancel-id",
        )

    handle = service.start(
        agent_id="demo",
        task="block",
        runner=runner,
        timeout_sec=30,
        metadata={"subagent_session_id": "sess-cancel", "workspace_rel_path": "subagents/cancel-id"},
    )
    job_id = handle["job_id"]
    assert started.wait(timeout=2.0)
    cancel_snap = service.cancel(job_id)
    assert cancel_snap.status == "cancelled"
    assert _wait_until_terminal(service, job_id) == "cancelled"
    collected = service.collect(job_id)
    assert collected["status"] == "cancelled"
    assert collected["subagent_session_id"] == "sess-cancel"
    assert collected["workspace_rel_path"] == "subagents/cancel-id"


def test_ac09_timeout_clears_running_tracker(tmp_path):
    """AC-09: timed_out jobs leave no in-process running handles."""

    def slow_runner(**kwargs: Any) -> JobResult:
        time.sleep(3.0)
        return JobResult(job_id=kwargs["job_id"], agent_id="arith", status="completed", summary="late")

    service, _runtime = _build_agent_service(tmp_path, runner_factory=slow_runner)
    handle = service.submit(agent_id="arith", task="slow", timeout_sec=1)
    job_id = handle["job_id"]
    assert _wait_until_terminal(service.job_service, job_id, timeout_sec=5) == "timed_out"
    time.sleep(2.5)
    assert job_id not in service.job_service.running_job_ids()


def test_ac10_submit_creates_subagents_dir_without_manifest(tmp_path):
    """AC-10: each submit creates ``subagents/{id}/`` and no ``manifest.json``."""

    def runner(**kwargs: Any) -> JobResult:
        return JobResult(
            job_id=kwargs["job_id"],
            agent_id="arith",
            status="completed",
            summary="ok",
            subagent_session_id=kwargs["subagent_session_id"],
            workspace_rel_path=kwargs["workspace_rel_path"],
        )

    service, runtime = _build_agent_service(tmp_path, runner_factory=runner)
    first = service.submit(agent_id="arith", task="a")
    second = service.submit(agent_id="arith", task="b")
    assert first["workspace_rel_path"] != second["workspace_rel_path"]
    for rel in (first["workspace_rel_path"], second["workspace_rel_path"]):
        ws = runtime.workspace_dir / rel
        assert ws.is_dir()
        assert not (ws / "manifest.json").exists()
    assert _wait_until_terminal(service.job_service, first["job_id"]) == "completed"
    assert _wait_until_terminal(service.job_service, second["job_id"]) == "completed"


def test_ac11_submit_subagent_signature_accepts_optional_workspace_rel_path():
    """AC-11 (Phase C): submit may accept optional ``workspace_rel_path`` for reuse."""
    params = inspect.signature(submit_subagent).parameters
    assert "workspace_rel_path" in params
    assert params["workspace_rel_path"].default is None
    assert "agent_id" in params
    assert "task" in params


def test_ac12_job_path_does_not_create_workers_dir(tmp_path):
    """AC-12: job subagent workspace must not create ``workers/.memory``."""
    workers_root = tmp_path / "parent_session" / "workers"
    assert not workers_root.exists()

    def runner(**kwargs: Any) -> JobResult:
        ws = Path(kwargs["workspace_dir"])
        assert not (ws / "workers").exists()
        return JobResult(job_id=kwargs["job_id"], agent_id="arith", status="completed", summary="ok")

    service, runtime = _build_agent_service(tmp_path, runner_factory=runner)
    handle = service.submit(agent_id="arith", task="no workers")
    _wait_until_terminal(service.job_service, handle["job_id"])
    assert not (runtime.workspace_dir / "workers").exists()


@pytest.mark.asyncio
async def test_ac14_subprocess_uses_parent_session_id_in_cmd(monkeypatch, tmp_path):
    """AC-14: child CLI ``--session-id`` must be the parent session, not job_id."""
    captured: dict[str, Any] = {}

    async def fake_run_with_cancel(self, *, cmd, **_kwargs):
        captured["cmd"] = list(cmd)
        return {
            "returncode": 0,
            "stdout": '{"assistant_reply":"ok","original_msg":{"final_answer":"1"},"subagent_final_state":{"k":"v"}}',
            "stderr": "",
        }

    monkeypatch.setattr(SubagentSubprocessRunner, "_run_with_cancel", fake_run_with_cancel)
    runner = SubagentSubprocessRunner()
    config_path = tmp_path / "cfg.yaml"
    config_path.write_text("AGENT_CONFIG: {name: x, description: y}\n", encoding="utf-8")
    workspace = tmp_path / "subagents" / "abc"
    workspace.mkdir(parents=True)
    await runner.run(
        query="q",
        config_path=config_path,
        workspace_dir=workspace,
        subagent_session_id="subagent_uuid",
        user_id="u1",
        parent_session_id="parent_sess_xyz",
        sub_id=7,
        timeout=30,
        sandbox=SimpleNamespace(wrap=lambda cmd, **kwargs: cmd),
    )
    cmd = captured["cmd"]
    session_idx = cmd.index("--session-id")
    assert cmd[session_idx + 1] == "parent_sess_xyz"
    assert "job-" not in cmd[session_idx + 1]


def test_ac15_stdout_parser_matches_collect_contract():
    """AC-15: stdout parser produces collect-compatible fields."""
    completed = {
        "returncode": 0,
        "stdout": ('{"assistant_reply":"done","original_msg":{"final_answer":"1"},"subagent_final_state":{"k":"v"}}'),
        "stderr": "",
    }
    outcome = _parse_job_subagent_completed(
        completed=completed,
        parent_session_id="parent",
        worker_sub_id=1,
    )
    assert isinstance(outcome, JobSubagentOutcome)
    assert outcome.frontend_msg == "done"
    assert isinstance(outcome.original_msg, dict)
    assert outcome.state == {"k": "v"}
    assert outcome.status == "completed"


def test_ac18_ac19_agent_id_resolution(tmp_path):
    """AC-18/19: yaml id wins; otherwise filename stem is used."""
    with_id = tmp_path / "stem.yaml"
    with_id.write_text(
        yaml.safe_dump({"AGENT_CONFIG": {"id": "custom_id", "name": "n", "description": "d"}}),
        encoding="utf-8",
    )
    no_id = tmp_path / "from_stem.yaml"
    no_id.write_text(yaml.safe_dump({"AGENT_CONFIG": {"name": "n", "description": "d"}}), encoding="utf-8")
    registry = AgentRegistry.from_subagent_configs([{"path": str(with_id)}, {"path": str(no_id)}])
    assert registry.resolve("custom_id").spec is not None
    assert registry.resolve("from_stem").spec is not None
    assert registry.resolve("from_stem").spec.id == "from_stem"
