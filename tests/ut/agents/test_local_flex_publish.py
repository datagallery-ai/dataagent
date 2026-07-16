from __future__ import annotations

from pathlib import Path
from threading import Event
from types import SimpleNamespace

from dataagent.core.agents.adapters.local_flex import LocalFlexAdapter
from dataagent.core.agents.registry import AgentSpec
from dataagent.core.agents.subagent_subprocess_runner import JobSubagentOutcome


class _Runner:
    def __init__(self, *, status: str) -> None:
        self.status = status
        self.kwargs: dict = {}

    async def run(self, **kwargs):
        self.kwargs = kwargs
        if self.status == "completed":
            Path(kwargs["workspace_dir"]).mkdir(parents=True, exist_ok=True)
            (Path(kwargs["workspace_dir"]) / "result.md").write_text("done", encoding="utf-8")
        return JobSubagentOutcome(
            original_msg={},
            frontend_msg="done",
            state={},
            status=self.status,
            error="failed" if self.status == "failed" else "",
        )


def _run_adapter(tmp_path: Path, *, status: str, output_sharing_enabled: bool = True):
    parent = tmp_path / "parent"
    workspace = parent / "subagents" / "sub-1"
    runner = _Runner(status=status)
    adapter = LocalFlexAdapter(runner=runner)
    runtime = SimpleNamespace(
        workspace_dir=parent,
        user_id="u1",
        session_id="s1",
        sandbox=SimpleNamespace(),
        get_all_config=lambda: {"AGENT_CONFIG": {"subagent_output_sharing": output_sharing_enabled}},
    )
    result = adapter.run(
        job_id="job-1",
        spec=AgentSpec(id="agent", name="Agent", description="", config_path=tmp_path / "agent.yaml"),
        task="make report",
        workspace_dir=workspace,
        subagent_session_id="sub-1",
        runtime=runtime,
        cancel_event=Event(),
        emit_event=lambda _: None,
    )
    return result, runner, parent


def test_completed_subagent_publishes_before_returning(tmp_path: Path) -> None:
    result, runner, parent = _run_adapter(tmp_path, status="completed")
    assert result.status == "completed"
    assert Path(result.published_path, "result.md").read_text(encoding="utf-8") == "done"
    assert runner.kwargs["subagent_output_dir"] == parent / "subagent_output"
    assert (parent / "subagent_output" / "manifest.json").is_file()


def test_failed_subagent_does_not_publish(tmp_path: Path) -> None:
    result, _, parent = _run_adapter(tmp_path, status="failed")
    assert result.status == "failed"
    assert result.published_path == ""
    assert not (parent / "subagent_output" / "manifest.json").exists()


def test_output_sharing_is_disabled_by_default(tmp_path: Path) -> None:
    result, runner, parent = _run_adapter(tmp_path, status="completed", output_sharing_enabled=False)
    assert result.status == "completed"
    assert result.published_path == ""
    assert result.published_artifacts == []
    assert runner.kwargs["subagent_output_dir"] is None
    assert not (parent / "subagent_output").exists()
