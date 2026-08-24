from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from langchain_core.messages import HumanMessage

from dataagent.core.flex.nodes import planner as planner_module
from dataagent.core.flex.nodes.planner import Planner, _dump_context_prompt_if_enabled


class _Runtime:
    def __init__(self, config: dict[str, Any]) -> None:
        self.env = SimpleNamespace(llm_configs={})
        self._config = config

    def get_all_config(self) -> dict[str, Any]:
        return self._config


def _state(tmp_workspace, *, sub_id: int, content: str, run_id: Any = 0) -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content=content)],
        "user_id": "u",
        "session_id": "s",
        "run_id": run_id,
        "sub_id": sub_id,
        "workspace": str(tmp_workspace),
        "curr_iter": 0,
    }


def test_context_dump_separates_main_and_subagent_when_memory_dir_is_shared(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATAAGENT_CONTEXT_DUMP", "1")
    config = {"WORKSPACE_POLICY": {"layout": {"session_memory_dir": ".memory"}}}
    runtime = _Runtime(config)

    _dump_context_prompt_if_enabled(
        [HumanMessage(content="main planner prompt")],
        _state(tmp_path, sub_id=0, content="main planner prompt"),
        runtime,
    )
    _dump_context_prompt_if_enabled(
        [HumanMessage(content="document recall subagent prompt")],
        _state(tmp_path, sub_id=7, content="document recall subagent prompt"),
        runtime,
    )

    main_dump = tmp_path / ".memory" / "context_dump" / "run_0" / "round_0.txt"
    subagent_dump = tmp_path / ".memory" / "context_dump_sub7" / "run_0" / "round_0.txt"

    assert main_dump.is_file()
    assert subagent_dump.is_file()
    assert "main planner prompt" in main_dump.read_text(encoding="utf-8")
    assert "document recall subagent prompt" in subagent_dump.read_text(encoding="utf-8")
    assert main_dump.stat().st_mode & 0o777 == 0o600
    assert main_dump.parent.stat().st_mode & 0o777 == 0o700


def test_context_dump_normalizes_run_id_and_rejects_traversal(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATAAGENT_CONTEXT_DUMP", "1")
    runtime = _Runtime({"WORKSPACE_POLICY": {"layout": {"session_memory_dir": ".memory"}}})
    messages = [HumanMessage(content="planner prompt")]

    _dump_context_prompt_if_enabled(messages, _state(tmp_path, sub_id=0, content="", run_id="7"), runtime)
    _dump_context_prompt_if_enabled(
        messages,
        _state(tmp_path, sub_id=0, content="", run_id="../../../../attacker"),
        runtime,
    )

    assert (tmp_path / ".memory" / "context_dump" / "run_7" / "round_0.txt").is_file()
    assert not (tmp_path / "attacker" / "round_0.txt").exists()


def test_planner_does_not_load_portrait_memory_when_state_requests_it(monkeypatch, tmp_path) -> None:
    """A state value cannot re-enable LLM-generated snapshot or profile prompt injection."""
    planner = object.__new__(Planner)
    planner.system_prompt = object()
    planner.user_prompt = object()
    captured: dict[str, Any] = {}

    def fail_if_memory_is_loaded(*args, **kwargs):
        raise AssertionError("portrait memory must remain disabled")

    def capture_prompt(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(planner_module, "_build_memory_str", fail_if_memory_is_loaded)
    monkeypatch.setattr(planner_module, "prepare_flex_planner_prompt", capture_prompt)

    planner._prepare_messages_to_process(
        {"enable_portrait": True},
        object(),
        SimpleNamespace(workspace_dir=tmp_path),
    )

    assert "memory" not in captured
