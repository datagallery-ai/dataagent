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
"""Unit tests for native DataAgent SDK session identity handling."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

import dataagent.interface.sdk.agent as sdk_agent_module
from dataagent.interface.sdk.agent import DataAgent


class _CaptureGraph:
    def __init__(self) -> None:
        self.thread_ids: list[str] = []
        self.configs: list[dict[str, Any]] = []

    async def ainvoke(self, graph_input: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Capture the native LangGraph thread id and return the input state."""
        configurable = config.get("configurable", {})
        thread_id = str(configurable.get("thread_id", ""))
        self.thread_ids.append(thread_id)
        self.configs.append(config)
        return {"messages": graph_input.get("messages", []), "thread_id": thread_id}

    async def astream(
        self,
        graph_input: dict[str, Any],
        config: dict[str, Any],
        **_kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Capture a streaming invocation and yield its normalized graph input."""
        result = await self.ainvoke(graph_input, config)
        yield result


class _MinimalConfig:
    def copy(self) -> _MinimalConfig:
        """Return the immutable test config."""
        return self

    def get(self, _key: str, default: Any = None) -> Any:
        """Return configured defaults."""
        return default

    def get_all(self) -> dict[str, Any]:
        """Return the raw empty test configuration."""
        return {}


def _build_sdk_probe_agent(monkeypatch: pytest.MonkeyPatch) -> tuple[DataAgent, _CaptureGraph]:
    agent = DataAgent(_MinimalConfig())
    graph = _CaptureGraph()

    async def _get_chat_agent(user_id: str, session_id: str) -> _CaptureGraph:
        return graph

    monkeypatch.setattr(agent, "_get_chat_agent", _get_chat_agent)
    return agent, graph


def test_chat_without_session_id_generates_new_id_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Implicit session IDs must not be cached on the DataAgent SDK instance."""
    uuids = iter(["uuid-one", "uuid-two"])
    monkeypatch.setattr(sdk_agent_module.uuid, "uuid4", lambda: next(uuids))
    agent, graph = _build_sdk_probe_agent(monkeypatch)

    first = asyncio.run(agent.chat("hello"))
    second = asyncio.run(agent.chat("again"))

    assert str(first.get("thread_id", "")).endswith("uuid-one")
    assert str(second.get("thread_id", "")).endswith("uuid-two")
    assert graph.thread_ids == [first.get("thread_id"), second.get("thread_id")]
    assert first.get("thread_id") != second.get("thread_id")


def test_chat_uses_initial_state_session_id_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers can keep multi-turn continuity through ``initial_state.session_id``."""
    agent, graph = _build_sdk_probe_agent(monkeypatch)

    result = asyncio.run(agent.chat("hello", initial_state={"session_id": "existing-session"}))

    assert result.get("thread_id") == "anonymous:existing-session"
    assert graph.thread_ids == ["anonymous:existing-session"]


def test_chat_preserves_native_runnable_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Native LangGraph run options survive SDK session configuration."""
    agent, graph = _build_sdk_probe_agent(monkeypatch)

    asyncio.run(
        agent.chat(
            "hello",
            session_id="session-a",
            checkpoint_id="checkpoint-a",
            config={
                "tags": ["sdk"],
                "recursion_limit": 42,
                "configurable": {"tenant": "tenant-a", "thread_id": "caller-thread"},
            },
        )
    )

    run_config = graph.configs[0]
    configurable = run_config.get("configurable", {})
    assert run_config.get("tags") == ["sdk"]
    assert run_config.get("recursion_limit") == 42
    assert configurable.get("tenant") == "tenant-a"
    assert configurable.get("thread_id") == "anonymous:session-a"
    assert configurable.get("checkpoint_id") == "checkpoint-a"


def test_astream_converts_string_input_to_message_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """String streaming input must use the same message-state protocol as ``chat``."""
    agent, graph = _build_sdk_probe_agent(monkeypatch)

    async def collect() -> list[dict[str, Any]]:
        return [chunk async for chunk in agent.astream("hello", session_id="stream-session")]

    chunks = asyncio.run(collect())

    assert len(chunks) == 1
    messages = chunks[0].get("messages", [])
    assert len(messages) == 1
    assert messages[0].content == "hello"
    assert graph.thread_ids == ["anonymous:stream-session"]
