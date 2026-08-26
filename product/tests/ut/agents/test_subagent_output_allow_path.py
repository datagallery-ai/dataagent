from __future__ import annotations

from pathlib import Path

import pytest

from dataagent.actions.tools.local_tool import sub_agent_entry


class _Config:
    def __init__(self) -> None:
        self.values: dict[str, object] = {"WORKSPACE.allow_path": ["/existing"]}

    def get(self, key: str, default=None):
        return self.values.get(key, default)

    def set(self, key: str, value) -> None:
        self.values[key] = value


class _Agent:
    def __init__(self) -> None:
        self.config = _Config()

    async def chat(self, *_args, **_kwargs):
        return {"ok": True}


@pytest.mark.asyncio
async def test_subagent_entry_appends_shared_output_to_allow_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent = _Agent()
    monkeypatch.setenv("DATAAGENT_SUBAGENT_OUTPUT_DIR", str(tmp_path / "subagent_output"))
    monkeypatch.setattr(sub_agent_entry.DataAgent, "from_config", lambda _path: agent)

    result = await sub_agent_entry._run_agent(
        "task",
        str(tmp_path / "agent.yaml"),
        user_id="u1",
        session_id="s1",
        sub_id=1,
    )

    assert result == {"ok": True}
    assert agent.config.get("WORKSPACE.allow_path") == ["/existing", str(tmp_path / "subagent_output")]
