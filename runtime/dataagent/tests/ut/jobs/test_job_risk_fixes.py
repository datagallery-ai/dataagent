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
"""Risk-focused tests for Job subsystem fixes (cancel, hydrate, isolation)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from langchain_core.messages import HumanMessage

from dataagent.actions.tools.local_tool.job_tools import _raise_if_job_collect_failed
from dataagent.actions.tools.local_tool.sandbox import NoopSandbox, reset_current_sandbox, set_current_sandbox
from dataagent.core.agents.service import AgentService
from dataagent.core.agents.subagent_subprocess_runner import (
    _load_job_workspace_hydrate_state,
    _parse_job_subagent_completed,
    _prepare_job_initial_state_file,
    _run_cancellable_subprocess_async,
)
from dataagent.core.errors import DataAgentError
from dataagent.core.flex.hooks.history_writer import save_messages
from dataagent.core.flex.nodes.executor import Executor
from dataagent.core.jobs.file_store import FileJobStore
from dataagent.core.jobs.models import JobResult
from dataagent.core.jobs.service import JobService
from dataagent.core.managers.action_manager.base import ErrorType, classify_exception
from dataagent.core.swarm.worker_result import build_timeout_result, parse_subagent_stdout


@pytest.mark.asyncio
async def test_cancellable_subprocess_honours_midflight_cancel():
    """Cancel set while the child is running must terminate the subprocess promptly."""
    cancel_event = Event()
    token = set_current_sandbox(NoopSandbox())

    async def _set_cancel_after_delay() -> None:
        await asyncio.sleep(0.4)
        cancel_event.set()

    watcher = asyncio.create_task(_set_cancel_after_delay())
    started = time.monotonic()
    try:
        completed = await _run_cancellable_subprocess_async(
            cmd=[sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=60,
            env=dict(os.environ),
            cancel_event=cancel_event,
            progress_callback=None,
            tool_call_id=None,
        )
    finally:
        await watcher
        reset_current_sandbox(token)

    elapsed = time.monotonic() - started
    assert elapsed < 5.0
    assert "cancelled" in str(completed.get("stderr") or "").lower()
    assert int(completed.get("returncode") or 0) != 0


@pytest.mark.asyncio
async def test_cancellable_subprocess_honours_timeout():
    """Subprocess timeout must kill a long-running child."""
    cancel_event = Event()
    token = set_current_sandbox(NoopSandbox())
    started = time.monotonic()
    try:
        completed = await _run_cancellable_subprocess_async(
            cmd=[sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=1,
            env=dict(os.environ),
            cancel_event=cancel_event,
            progress_callback=None,
            tool_call_id=None,
        )
    finally:
        reset_current_sandbox(token)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0
    assert completed.get("timed_out") is True


def test_job_timeout_flag_maps_to_subagent_002() -> None:
    timeout_wr = build_timeout_result(sub_id=7, parent_session_id="parent", timeout=30)
    outcome = _parse_job_subagent_completed(
        completed={"stdout": "", "stderr": "unrelated", "returncode": -1, "timed_out": True},
        parent_session_id="parent",
        worker_sub_id=7,
        timeout=30,
    )
    assert outcome.original_msg["error"]["source"] == "constraint"
    assert "http_status" not in outcome.original_msg["error"]
    assert outcome.original_msg["status"] == "timeout"
    assert isinstance(outcome.error, dict)
    assert outcome.error["source"] == "constraint"
    assert outcome.error["component"] == "subagent"
    assert outcome.error["trace_id"]
    assert timeout_wr.error.source == "constraint"
    assert "7" in timeout_wr.error.fact
    assert "30" in timeout_wr.error.fact
    restored = DataAgentError.from_dict(timeout_wr.to_dict()["error"])
    assert "7" in restored.fact
    assert "30" in restored.fact
    assert "locator" not in timeout_wr.to_dict()["error"]


def test_job_timeout_collect_does_not_synthesize_cause_for_retry() -> None:
    """Subprocess timed_out restores four fields only; retry does not use a synthetic cause."""
    outcome = _parse_job_subagent_completed(
        completed={"stdout": "", "stderr": "unrelated", "returncode": -1, "timed_out": True},
        parent_session_id="parent",
        worker_sub_id=7,
        timeout=30,
    )
    assert outcome.status == "timed_out"
    assert isinstance(outcome.error, dict)

    with pytest.raises(DataAgentError) as caught:
        _raise_if_job_collect_failed(
            {
                "status": outcome.status,
                "error": outcome.error,
                "job_id": "job-timeout",
                "summary": outcome.frontend_msg,
            },
            tool_name="collect_subagent",
        )

    error = caught.value
    assert not isinstance(error.__cause__, TimeoutError)
    assert classify_exception(error)[0] == ErrorType.UNKNOWN
    executor = Executor("executor")
    policy = executor._retry_policy_for(error)
    assert policy.error_type == ErrorType.UNKNOWN
    assert executor._should_retry(error) is False


def test_job_does_not_classify_timeout_from_stderr_text() -> None:
    outcome = _parse_job_subagent_completed(
        completed={"stdout": "", "stderr": "subagent subprocess timed out", "returncode": -1},
        parent_session_id="parent",
        worker_sub_id=7,
        timeout=30,
    )
    assert outcome.original_msg["error"]["source"] == "tool"
    assert outcome.original_msg["error"]["component"] == "subagent"


def test_swarm_and_job_share_stdout_parser() -> None:
    parsed = parse_subagent_stdout(
        json.dumps(
            {
                "worker_result": {
                    "sub_id": 3,
                    "parent_session_id": "p",
                    "worker_session_id": "subagent_p_3",
                    "status": "success",
                    "final_answer": "ok",
                    "artifacts": [],
                    "tool_calls_count": 0,
                    "iteration_count": 0,
                    "error": None,
                    "resumed": False,
                }
            }
        ),
        sub_id=3,
        parent_session_id="p",
    )
    assert parsed.worker_result is not None
    assert parsed.worker_result.status == "success"
    assert parsed.worker_result.final_answer == "ok"


def test_prepare_job_initial_state_file_hydrates_prior_messages(tmp_path):
    """Reused workspaces must load prior ``messages.json`` into the initial state file."""
    workspace = tmp_path / "subagents" / "sess-1"
    workspace.mkdir(parents=True)
    save_messages("u1", "sess-1", [HumanMessage(content="prior turn")], workspace=workspace)

    state_path = _prepare_job_initial_state_file(
        workspace_dir=workspace,
        subagent_session_id="sess-1",
        user_id="u1",
        parent_session_id="parent",
        sub_id=123456,
        query="follow-up task",
        reuse_workspace=True,
    )
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["content"] == "prior turn"
    assert payload["user_query"] == "follow-up task"
    assert payload["run_id"] >= 1


def test_load_job_workspace_hydrate_state_increments_run_id_from_snapshot(tmp_path):
    """Snapshot ``run_id`` seeds the next hydrated ``run_id``."""
    workspace = tmp_path / "subagents" / "sess-2"
    mem = workspace / ".memory"
    mem.mkdir(parents=True)
    (mem / "snapshot.json").write_text(
        json.dumps({"user_snapshot": {"run_id": 3, "session_summary": "done"}}),
        encoding="utf-8",
    )

    messages, base_state, next_run_id = _load_job_workspace_hydrate_state(workspace)
    assert messages == []
    assert next_run_id == 4
    assert "user_snapshot" in base_state


def test_job_service_running_ids_are_instance_scoped(tmp_path):
    """Each ``JobService`` instance tracks only its own in-process jobs."""
    store_a = FileJobStore(tmp_path / "ws-a")
    store_b = FileJobStore(tmp_path / "ws-b")
    service_a = JobService(store_a)
    service_b = JobService(store_b)
    started = Event()

    def runner(job_id: str, cancel_event: Event) -> JobResult:
        started.set()
        while not cancel_event.is_set():
            time.sleep(0.05)
        return JobResult(job_id=job_id, agent_id="demo", status="cancelled", summary="cancelled")

    handle = service_a.start(agent_id="demo", task="block", runner=runner, timeout_sec=30)
    assert started.wait(timeout=2.0)
    assert handle["job_id"] in service_a.running_job_ids()
    assert handle["job_id"] not in service_b.running_job_ids()
    service_a.cancel(handle["job_id"])


def test_job_service_shutdown_kills_registered_child_process(tmp_path):
    """shutdown() must kill a registered child process group even if the runner stalls."""
    store = FileJobStore(tmp_path)
    service = JobService(store)
    child_started = Event()
    child_proc_box: list[Any] = []

    def runner(job_id: str, cancel_event: Event) -> JobResult:
        proc = __import__("subprocess").Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        child_proc_box.append(proc)
        service.register_child_pgid(job_id, int(proc.pid))
        child_started.set()
        # Do not kill the child here; shutdown() must killpg it.
        while not cancel_event.is_set():
            time.sleep(0.05)
        return JobResult(job_id=job_id, agent_id="demo", status="cancelled", summary="cancelled")

    handle = service.start(agent_id="demo", task="orphan-child", runner=runner, timeout_sec=60)
    assert child_started.wait(timeout=2.0)
    child_proc = child_proc_box[0]

    service.shutdown()

    deadline = time.time() + 3.0
    while time.time() < deadline and child_proc.poll() is None:
        time.sleep(0.05)
    assert child_proc.poll() is not None
    assert service.poll(handle["job_id"]).status == "cancelled"


def test_job_service_cancel_kills_registered_child_process(tmp_path):
    """cancel() must killpg the registered child immediately."""
    store = FileJobStore(tmp_path)
    service = JobService(store)
    child_started = Event()
    child_proc_box: list[Any] = []

    def runner(job_id: str, cancel_event: Event) -> JobResult:
        proc = __import__("subprocess").Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        child_proc_box.append(proc)
        service.register_child_pgid(job_id, int(proc.pid))
        child_started.set()
        while not cancel_event.is_set():
            time.sleep(0.05)
        service.clear_child_pgid(job_id)
        return JobResult(job_id=job_id, agent_id="demo", status="cancelled", summary="cancelled")

    handle = service.start(agent_id="demo", task="cancel-child", runner=runner, timeout_sec=60)
    assert child_started.wait(timeout=2.0)
    child_proc = child_proc_box[0]
    service.cancel(handle["job_id"])

    deadline = time.time() + 3.0
    while time.time() < deadline and child_proc.poll() is None:
        time.sleep(0.05)
    assert child_proc.poll() is not None


def test_job_service_cancel_kills_orphans_by_subagent_workspace_cwd(tmp_path):
    """cancel() must kill orphans whose cwd is under the subagent workspace."""
    parent_ws = tmp_path / "parent"
    sub_ws = parent_ws / "subagents" / "sess-cwd"
    sub_ws.mkdir(parents=True)
    store = FileJobStore(parent_ws)
    service = JobService(store)
    orphan_started = Event()
    orphan_box: list[Any] = []

    def runner(job_id: str, cancel_event: Event) -> JobResult:
        # Nested start_new_session: not covered by register_child_pgid killpg alone.
        proc = __import__("subprocess").Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=str(sub_ws),
            start_new_session=True,
        )
        orphan_box.append(proc)
        orphan_started.set()
        while not cancel_event.is_set():
            time.sleep(0.05)
        return JobResult(
            job_id=job_id,
            agent_id="demo",
            status="cancelled",
            summary="cancelled",
            workspace_rel_path="subagents/sess-cwd",
        )

    handle = service.start(
        agent_id="demo",
        task="cwd-orphan",
        runner=runner,
        timeout_sec=60,
        metadata={"workspace_rel_path": "subagents/sess-cwd", "subagent_session_id": "sess-cwd"},
    )
    assert orphan_started.wait(timeout=2.0)
    orphan = orphan_box[0]
    assert orphan.poll() is None

    service.cancel(handle["job_id"])
    deadline = time.time() + 3.0
    while time.time() < deadline and orphan.poll() is None:
        time.sleep(0.05)
    assert orphan.poll() is not None
    assert service.poll(handle["job_id"]).status == "cancelled"


def test_terminate_processes_with_cwd_under_only_targets_subtree(tmp_path):
    """cwd sweep must hit the scoped workspace and leave sibling cwd processes alone."""
    from dataagent.core.utils.subprocess import terminate_processes_with_cwd_under

    target = tmp_path / "subagents" / "a"
    sibling = tmp_path / "subagents" / "b"
    target.mkdir(parents=True)
    sibling.mkdir(parents=True)
    target_proc = __import__("subprocess").Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(target),
        start_new_session=True,
    )
    sibling_proc = __import__("subprocess").Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(sibling),
        start_new_session=True,
    )
    try:
        assert target_proc.poll() is None
        assert sibling_proc.poll() is None
        killed = terminate_processes_with_cwd_under(target)
        assert target_proc.pid in killed
        deadline = time.time() + 3.0
        while time.time() < deadline and target_proc.poll() is None:
            time.sleep(0.05)
        assert target_proc.poll() is not None
        assert sibling_proc.poll() is None
    finally:
        for proc in (target_proc, sibling_proc):
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(proc.pid, signal.SIGKILL)


def test_resolve_subagent_kill_workspace_rejects_parent_and_bare_subagents(tmp_path):
    """Only concrete subagents/<id> paths are accepted for cwd-scoped cleanup."""
    from dataagent.core.jobs.service import _resolve_subagent_kill_workspace

    parent = tmp_path / "parent"
    (parent / "subagents" / "sess").mkdir(parents=True)
    assert (
        _resolve_subagent_kill_workspace(parent, {"workspace_rel_path": "subagents/sess"})
        == (parent / "subagents" / "sess").resolve()
    )
    assert _resolve_subagent_kill_workspace(parent, {"workspace_rel_path": "."}) is None
    assert _resolve_subagent_kill_workspace(parent, {"workspace_rel_path": "subagents"}) is None
    assert _resolve_subagent_kill_workspace(parent, {"workspace_rel_path": "artifacts/x"}) is None
    assert _resolve_subagent_kill_workspace(parent, {}) is None


@pytest.mark.asyncio
async def test_cancellable_subprocess_registers_child_lifecycle_callbacks():
    """Child start/finish callbacks must fire around a cancellable subprocess."""
    cancel_event = Event()
    token = set_current_sandbox(NoopSandbox())
    started: list[int] = []
    finished = Event()

    async def _set_cancel_after_delay() -> None:
        await asyncio.sleep(0.3)
        cancel_event.set()

    watcher = asyncio.create_task(_set_cancel_after_delay())
    try:
        completed = await _run_cancellable_subprocess_async(
            cmd=[sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=60,
            env=dict(os.environ),
            cancel_event=cancel_event,
            progress_callback=None,
            tool_call_id=None,
            on_child_started=started.append,
            on_child_finished=finished.set,
        )
    finally:
        await watcher
        reset_current_sandbox(token)

    assert started and started[0] > 0
    assert finished.is_set()
    assert "cancelled" in str(completed.get("stderr") or "").lower()


def test_completed_collect_includes_required_business_keys(tmp_path):
    """Completed jobs must always expose AC-07 business keys in collect output."""
    store = FileJobStore(tmp_path)
    service = JobService(store)

    def runner(job_id: str, _cancel_event: Event) -> JobResult:
        return JobResult(
            job_id=job_id,
            agent_id="demo",
            status="completed",
            summary="",
            original_msg=None,
            frontend_msg="",
            state=None,
            subagent_session_id="sess",
            workspace_rel_path="subagents/sess",
        )

    handle = service.start(agent_id="demo", task="t", runner=runner, timeout_sec=5)
    job_id = handle["job_id"]
    deadline = time.time() + 5
    while time.time() < deadline:
        if service.poll(job_id).status == "completed":
            break
        time.sleep(0.05)

    collected = service.collect(job_id)
    assert collected["status"] == "completed"
    assert "original_msg" in collected
    assert "frontend_msg" in collected
    assert "state" in collected
    assert collected["subagent_session_id"] == "sess"
    assert collected["workspace_rel_path"] == "subagents/sess"


def test_agent_service_reuse_passes_hydrate_flag_to_runner(tmp_path, monkeypatch):
    """Second submit on the same workspace must request hydrated initial state."""
    captured: list[bool] = []

    class _RecordingRunner:
        async def run(self, **kwargs):
            captured.append(bool(kwargs.get("reuse_workspace")))
            return type(
                "Outcome",
                (),
                {
                    "original_msg": {"ok": True},
                    "frontend_msg": "ok",
                    "state": {},
                    "status": "completed",
                    "error": "",
                },
            )()

    monkeypatch.setattr(
        "dataagent.core.agents.adapters.local_flex.SubagentSubprocessRunner",
        lambda *args, **kwargs: _RecordingRunner(),
    )

    parent_ws = tmp_path / "parent"
    parent_ws.mkdir()
    subagent_yaml = tmp_path / "demo.yaml"
    subagent_yaml.write_text(
        yaml.safe_dump({"AGENT_CONFIG": {"id": "demo", "name": "demo", "description": "d"}}),
        encoding="utf-8",
    )

    from dataagent.core.agents.registry import AgentRegistry

    registry = AgentRegistry.from_subagent_configs([{"path": str(subagent_yaml)}])
    job_service = JobService(FileJobStore(parent_ws))
    runtime = SimpleNamespace(
        workspace_dir=parent_ws,
        session_id="parent_sess",
        user_id="u1",
        sandbox=SimpleNamespace(wrap=lambda cmd, **kwargs: cmd),
        on_subagent_progress=None,
        env=SimpleNamespace(config_manager=SimpleNamespace(get=lambda *_a, **_k: 4)),
        get_all_config=lambda: {},
    )

    service = AgentService(registry=registry, job_service=job_service, runtime=runtime)
    first = service.submit(agent_id="demo", task="first")
    assert first["status"] != "ERROR"
    deadline = time.time() + 5
    while time.time() < deadline:
        if job_service.poll(first["job_id"]).status == "completed":
            break
        time.sleep(0.05)

    second = service.submit(
        agent_id="demo",
        task="second",
        job_envelope={"workspace_rel_path": first["workspace_rel_path"]},
    )
    assert second["reused_workspace"] is True
    deadline = time.time() + 5
    while time.time() < deadline and len(captured) < 2:
        time.sleep(0.05)
    assert captured == [False, True]
