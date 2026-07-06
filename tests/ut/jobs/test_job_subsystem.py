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
"""Unit tests for Ferry Job subsystem (Phase A)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from langchain_core.messages import HumanMessage

from dataagent.core.agents.registry import AgentRegistry, resolve_agent_id_from_yaml
from dataagent.core.agents.subagent_session import (
    prepare_subagent_workspace,
    resolve_subagent_workspace_session,
)
from dataagent.core.flex.hooks.history_writer import save_messages
from dataagent.core.jobs.file_store import FileJobStore
from dataagent.core.jobs.models import JobResult
from dataagent.core.jobs.service import JobService
from dataagent.core.managers.action_manager.manager import ToolManager
from dataagent.utils.runtime_paths import (
    FLEX_PERSISTENCE_ROOT_ENV,
    is_job_subagent_workspace,
    resolve_flex_storage_root,
)


def test_resolve_agent_id_prefers_yaml_id(tmp_path):
    """``agent_id`` resolves from ``AGENT_CONFIG.id`` before filename stem."""
    path = tmp_path / "arithmetic_ref.yaml"
    path.write_text(
        yaml.safe_dump({"AGENT_CONFIG": {"id": "arith", "name": "n", "description": "d"}}),
        encoding="utf-8",
    )
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert resolve_agent_id_from_yaml(path, payload) == "arith"


def test_prepare_subagent_workspace_creates_directory(tmp_path):
    """Each submit allocates a new workspace under ``subagents/``."""
    session = prepare_subagent_workspace(parent_workspace=tmp_path)
    assert session.workspace_dir.is_dir()
    assert session.workspace_rel_path.startswith("subagents/")
    assert (tmp_path / session.workspace_rel_path).exists()
    assert is_job_subagent_workspace(session.workspace_dir)


def test_prepare_subagent_workspace_honors_layout_subagents_dir(tmp_path):
    """``WORKSPACE_POLICY.layout.subagents_dir`` overrides the default parent folder."""
    config = {"WORKSPACE_POLICY": {"layout": {"subagents_dir": ".dataagent/subagents"}}}
    session = prepare_subagent_workspace(parent_workspace=tmp_path, config=config)
    assert session.workspace_rel_path.startswith(".dataagent/subagents/")
    assert is_job_subagent_workspace(session.workspace_dir, config=config)
    assert (tmp_path / session.workspace_rel_path).is_dir()


def test_file_job_store_honors_layout_jobs_dir(tmp_path):
    """``WORKSPACE_POLICY.layout.jobs_dir`` overrides the default jobs parent folder."""
    config = {"WORKSPACE_POLICY": {"layout": {"jobs_dir": "state/jobs"}}}
    store = FileJobStore(tmp_path, config=config)
    assert store.jobs_root() == (tmp_path / "state" / "jobs").resolve()


def test_resolve_subagent_workspace_session_reuses_existing_directory(tmp_path):
    """``workspace_rel_path`` binds an existing job subagent directory."""
    first = prepare_subagent_workspace(parent_workspace=tmp_path)
    reused = resolve_subagent_workspace_session(
        parent_workspace=tmp_path,
        workspace_rel_path=first.workspace_rel_path,
    )
    assert reused.workspace_dir == first.workspace_dir
    assert reused.subagent_session_id == first.subagent_session_id


def test_resolve_subagent_workspace_session_rejects_missing_directory(tmp_path):
    """Missing reuse paths must fail validation."""
    with pytest.raises(ValueError, match="does not exist"):
        resolve_subagent_workspace_session(
            parent_workspace=tmp_path,
            workspace_rel_path="subagents/missing-id",
        )


def test_agent_service_submit_reuses_workspace_after_prior_job(tmp_path):
    """A second submit with ``workspace_rel_path`` reuses the same workspace directory."""
    captured_dirs: list[Path] = []

    def runner(**kwargs: Any) -> JobResult:
        captured_dirs.append(Path(kwargs["workspace_dir"]))
        ws = Path(kwargs["workspace_dir"])
        mem_dir = ws / ".memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        (mem_dir / "messages.json").write_text(
            '{"messages": [{"type": "HumanMessage", "content": "prior", "name": "", '
            '"additional_kwargs": {}, "response_metadata": {}}]}',
            encoding="utf-8",
        )
        return JobResult(
            job_id=kwargs["job_id"],
            agent_id="demo",
            status="completed",
            summary="done",
            subagent_session_id=kwargs["subagent_session_id"],
            workspace_rel_path=kwargs["workspace_rel_path"],
        )

    from dataagent.core.agents.service import AgentService

    parent_ws = tmp_path / "parent_session"
    parent_ws.mkdir(parents=True, exist_ok=True)
    store = FileJobStore(parent_ws)
    job_service = JobService(store)
    runtime = type(
        "Runtime",
        (),
        {
            "workspace_dir": parent_ws,
            "session_id": "parent_sess",
            "user_id": "u1",
            "sandbox": type("Sandbox", (), {"wrap": staticmethod(lambda cmd, **kwargs: cmd)})(),
            "on_subagent_progress": None,
            "env": type("Env", (), {"config_manager": type("CM", (), {"get": staticmethod(lambda *_a, **_k: 4)})()})(),
            "get_all_config": staticmethod(lambda: {}),
        },
    )()

    subagent_yaml = tmp_path / "demo.yaml"
    subagent_yaml.write_text(
        yaml.safe_dump({"AGENT_CONFIG": {"id": "demo", "name": "demo", "description": "d"}}),
        encoding="utf-8",
    )
    registry = AgentRegistry.from_subagent_configs([{"path": str(subagent_yaml)}])

    class _Adapter:
        def run(self, **kwargs: Any) -> JobResult:
            return runner(**kwargs)

    service = AgentService(registry=registry, job_service=job_service, runtime=runtime, adapter=_Adapter())
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
    assert second["workspace_rel_path"] == first["workspace_rel_path"]
    assert second["subagent_session_id"] == first["subagent_session_id"]
    deadline = time.time() + 5
    while time.time() < deadline:
        if job_service.poll(second["job_id"]).status == "completed":
            break
        time.sleep(0.05)
    assert len(captured_dirs) == 2
    assert captured_dirs[0] == captured_dirs[1]


def test_agent_service_submit_rejects_busy_reused_workspace(tmp_path):
    """Concurrent reuse of the same workspace must be rejected."""
    from threading import Event

    from dataagent.core.agents.service import AgentService

    started = Event()

    def runner(**kwargs: Any) -> JobResult:
        started.set()
        Event().wait(10)
        return JobResult(job_id=kwargs["job_id"], agent_id="demo", status="completed", summary="late")

    parent_ws = tmp_path / "parent_session"
    parent_ws.mkdir(parents=True, exist_ok=True)
    store = FileJobStore(parent_ws)
    job_service = JobService(store)
    runtime = type(
        "Runtime",
        (),
        {
            "workspace_dir": parent_ws,
            "session_id": "parent_sess",
            "user_id": "u1",
            "sandbox": type("Sandbox", (), {"wrap": staticmethod(lambda cmd, **kwargs: cmd)})(),
            "on_subagent_progress": None,
            "env": type("Env", (), {"config_manager": type("CM", (), {"get": staticmethod(lambda *_a, **_k: 4)})()})(),
            "get_all_config": staticmethod(lambda: {}),
        },
    )()
    subagent_yaml = tmp_path / "demo.yaml"
    subagent_yaml.write_text(
        yaml.safe_dump({"AGENT_CONFIG": {"id": "demo", "name": "demo", "description": "d"}}),
        encoding="utf-8",
    )
    registry = AgentRegistry.from_subagent_configs([{"path": str(subagent_yaml)}])

    class _Adapter:
        def run(self, **kwargs: Any) -> JobResult:
            return runner(**kwargs)

    service = AgentService(registry=registry, job_service=job_service, runtime=runtime, adapter=_Adapter())
    first = service.submit(agent_id="demo", task="block")
    assert first["status"] != "ERROR"
    deadline = time.time() + 2
    while time.time() < deadline and not started.is_set():
        time.sleep(0.02)
    second = service.submit(
        agent_id="demo",
        task="second",
        job_envelope={"workspace_rel_path": first["workspace_rel_path"]},
    )
    assert second["status"] == "ERROR"
    assert "busy" in second["message"]
    job_service.cancel(first["job_id"])


def test_flex_storage_root_uses_job_subagent_workspace_env(tmp_path, monkeypatch):
    """Job subprocess persistence root redirects ``.memory`` under ``subagents/{id}/``."""
    parent = tmp_path / "parent_session"
    session = prepare_subagent_workspace(parent_workspace=parent)
    monkeypatch.setenv(FLEX_PERSISTENCE_ROOT_ENV, str(session.workspace_dir))
    storage_root = resolve_flex_storage_root(user_id="anonymous", session_id="ignored")
    assert storage_root == session.workspace_dir.resolve()

    save_messages("anonymous", session.subagent_session_id, [HumanMessage(content="hi")])
    messages_path = session.workspace_dir / ".memory" / "messages.json"
    assert messages_path.is_file()
    assert not (tmp_path / "anonymous" / "ignored" / ".memory" / "messages.json").exists()


def test_job_service_lifecycle_writes_result(tmp_path):
    """JobService persists status/events/result under ``jobs/{job_id}/``."""
    store = FileJobStore(tmp_path)
    service = JobService(store)

    def runner(job_id: str, cancel_event) -> JobResult:
        return JobResult(
            job_id=job_id,
            agent_id="demo",
            status="completed",
            summary="done",
            original_msg={"final_answer": "42"},
            frontend_msg="42",
            state={"complete": True},
            subagent_session_id="sess123",
        )

    handle = service.start(
        agent_id="demo",
        task="1+1",
        runner=runner,
        timeout_sec=5,
        metadata={"workspace_rel_path": "subagents/demo-id", "subagent_session_id": "sess123"},
    )
    job_id = handle["job_id"]

    deadline = time.time() + 5
    while time.time() < deadline:
        snap = service.poll(job_id)
        if snap.status == "completed":
            break
        time.sleep(0.05)
    collected = service.collect(job_id)
    assert collected["status"] == "completed"
    assert collected["original_msg"] == {"final_answer": "42"}
    assert collected["frontend_msg"] == "42"
    assert collected["state"] == {"complete": True}
    assert collected["subagent_session_id"] == "sess123"
    assert collected["workspace_rel_path"] == "subagents/demo-id"
    assert store.job_json_path(job_id).exists()
    assert store.result_json_path(job_id).exists()


@pytest.mark.asyncio
async def test_implicit_job_tools_registered_from_subagent_configs(tmp_path):
    """``SUBAGENT_CONFIGS`` registers the four job lifecycle tools."""
    subagent_yaml = tmp_path / "worker.yaml"
    subagent_yaml.write_text(
        yaml.safe_dump({"AGENT_CONFIG": {"name": "arith", "description": "does math"}}),
        encoding="utf-8",
    )
    tm = ToolManager()
    tm._register_implicit_job_tools({"SUBAGENT_CONFIGS": [{"path": str(subagent_yaml)}]})
    for name in ("submit_subagent", "poll_subagent", "collect_subagent", "cancel_subagent"):
        assert tm.exists(name)
    assert not tm.exists("sub_agent_tool")
    desc = tm.get("submit_subagent").description
    assert "worker" in desc or "worker.yaml" in desc or "arith" in desc
    await tm.cleanup()


def test_agent_registry_from_subagent_configs(tmp_path):
    """Registry maps ``agent_id`` to config path."""
    subagent_yaml = tmp_path / "arithmetic_ref.yaml"
    subagent_yaml.write_text(
        yaml.safe_dump({"AGENT_CONFIG": {"name": "arith", "description": "does math"}}),
        encoding="utf-8",
    )
    registry = AgentRegistry.from_subagent_configs([{"path": str(subagent_yaml)}])
    resolution = registry.resolve("arithmetic_ref")
    assert resolution.spec is not None
    assert resolution.spec.id == "arithmetic_ref"
    assert resolution.spec.config_path == subagent_yaml
