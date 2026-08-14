# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Unit tests for DataAgent SDK session identity handling."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import dataagent.interface.sdk.agent as sdk_agent_module
from dataagent.interface.sdk.agent import DataAgent


class _CaptureChatAgent:
    def __init__(self) -> None:
        self.session_ids: list[str] = []

    async def chat(
        self, user_query: str, *, session_id: str, initial_state: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        self.session_ids.append(session_id)
        return {"user_query": user_query, "session_id": session_id, "initial_state": initial_state}


class _MinimalConfig:
    def copy(self) -> _MinimalConfig:
        return self

    def get(self, _key: str, default: Any = None) -> Any:
        return default

    def get_all(self) -> dict[str, Any]:
        return {}


def _build_sdk_probe_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[DataAgent, _CaptureChatAgent]:
    monkeypatch.setattr(DataAgent, "global_init", lambda _self, _config: None)
    agent = DataAgent(_MinimalConfig())
    chat_agent = _CaptureChatAgent()
    agent._chat_agent_instance = chat_agent

    def initialize_state(
        initial_state: dict[str, Any] | None = None,
        session_id: str | None = None,
        workspace: Path | str | None = None,
    ) -> dict[str, Any]:
        state = dict(initial_state or {})
        state.setdefault("workspace", tmp_path)
        state.setdefault("user_id", "ut-user")
        state["session_id"] = session_id
        return state

    agent._initialize_state = initialize_state
    agent._ensure_workspace = lambda _state: None
    agent._touch_workspace_catalog = lambda _state: None
    agent._dump_runtime_config = lambda _state: None
    monkeypatch.setattr(sdk_agent_module, "setup_session_log", lambda **_kwargs: None)
    return agent, chat_agent


def test_chat_without_session_id_generates_new_id_per_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Implicit session IDs must not be cached on the DataAgent SDK instance."""
    uuids = iter(["uuid-one", "uuid-two"])
    monkeypatch.setattr(sdk_agent_module.uuid, "uuid4", lambda: next(uuids))
    agent, chat_agent = _build_sdk_probe_agent(monkeypatch, tmp_path)

    first = asyncio.run(agent.chat("hello"))
    second = asyncio.run(agent.chat("again"))

    assert first["session_id"].endswith("uuid-one")
    assert second["session_id"].endswith("uuid-two")
    assert chat_agent.session_ids == [first["session_id"], second["session_id"]]
    assert first["session_id"] != second["session_id"]
    assert not hasattr(agent, "session_id")


def test_chat_uses_initial_state_session_id_when_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Existing callers can keep multi-turn continuity by passing initial_state.session_id."""
    agent, chat_agent = _build_sdk_probe_agent(monkeypatch, tmp_path)

    result = asyncio.run(agent.chat("hello", initial_state={"session_id": "existing-session"}))

    assert result["session_id"] == "existing-session"
    assert chat_agent.session_ids == ["existing-session"]
