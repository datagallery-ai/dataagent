from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from langchain_core.messages import HumanMessage

from dataagent.core.flex.nodes.planner import _dump_context_prompt_if_enabled


class _Runtime:
    def __init__(self, config: dict[str, Any]) -> None:
        self.env = SimpleNamespace(llm_configs={})
        self._config = config

    def get_all_config(self) -> dict[str, Any]:
        return self._config


def _state(tmp_workspace, *, sub_id: int, content: str) -> dict[str, Any]:
    return {
        "messages": [HumanMessage(content=content)],
        "user_id": "u",
        "session_id": "s",
        "run_id": 0,
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
