# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Checkpoint adapter and DataAgent session lifecycle tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dataagent.core.deep_agent.checkpoint import (
    CheckpointerSpec,
    build_checkpointer_spec,
    checkpointer_lease,
)
from dataagent.interface.sdk import agent as agent_module


def test_builds_default_in_memory_spec(tmp_path: Path) -> None:
    spec = build_checkpointer_spec({}, workspace_root=tmp_path)

    assert spec == CheckpointerSpec(type="in_memory", conf={})


def test_resolves_relative_persistence_path_under_workspace(tmp_path: Path) -> None:
    spec = build_checkpointer_spec(
        {
            "CHECKPOINTER": {
                "type": "persistence",
                "conf": {"db_type": "sqlite", "db_path": "state/checkpoints"},
            }
        },
        workspace_root=tmp_path,
    )

    assert spec.type == "persistence"
    assert spec.conf == {
        "db_type": "sqlite",
        "db_path": str((tmp_path / "state" / "checkpoints").resolve()),
    }


def test_accepts_legacy_checkpoint_section_alias(tmp_path: Path) -> None:
    spec = build_checkpointer_spec(
        {"CHECKPOINT": {"type": "persistence", "conf": {"db_type": "shelve"}}},
        workspace_root=tmp_path,
    )

    assert spec.type == "persistence"
    assert spec.conf == {
        "db_type": "shelve",
        "db_path": str((tmp_path / ".checkpoints" / "dataagent").resolve()),
    }


def test_rejects_invalid_redis_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires url or redis_client"):
        build_checkpointer_spec(
            {"CHECKPOINTER": {"type": "redis", "conf": {"connection": {}}}},
            workspace_root=tmp_path,
        )


def test_checkpoint_id_is_session_alias(tmp_path: Path) -> None:
    agent = agent_module.DataAgent({"WORKSPACE": {"path": str(tmp_path)}})

    assert (
        agent._resolve_checkpoint_session_id(
            session_id=None,
            checkpoint_id="checkpoint-1",
            initial_state=None,
        )
        == "checkpoint-1"
    )
    assert (
        agent._resolve_checkpoint_session_id(
            session_id=None,
            checkpoint_id=None,
            initial_state={"checkpoint_id": "state-checkpoint"},
        )
        == "state-checkpoint"
    )
    with pytest.raises(ValueError, match="both values must match"):
        agent._resolve_checkpoint_session_id(
            session_id="session-1",
            checkpoint_id="checkpoint-1",
            initial_state=None,
        )


class _FakeSession:
    def __init__(self, session_id: str, card: Any) -> None:
        self.session_id = session_id
        self.card = card
        self.events: list[str] = []

    async def pre_run(self, **kwargs: Any) -> None:
        self.events.append("pre_run")

    async def post_run(self) -> None:
        self.events.append("post_run")


class _FakeDeepAgent:
    def __init__(self) -> None:
        self.card = SimpleNamespace(id="dataagent-card", name="dataagent-card")
        self.session: _FakeSession | None = None

    async def invoke(self, inputs: dict[str, Any], session: _FakeSession):
        self.session = session
        session.events.append("invoke")
        return {"output": "done"}

    async def stream(self, inputs: dict[str, Any], session: _FakeSession):
        self.session = session
        yield {"output": "streamed"}


class _SchemaDictStreamDeepAgent(_FakeDeepAgent):
    async def stream(self, inputs: dict[str, Any], session: _FakeSession):
        """Yield Jiuwen OutputSchema-like dict chunks."""
        self.session = session
        yield {"type": "llm_output", "index": 0, "payload": {"content": "hello"}}
        yield {"type": "llm_usage", "index": 1, "payload": {"usage_metadata": {"total_tokens": 1}}}
        yield {"type": "answer", "index": 1, "payload": {"output": "hello", "result_type": "answer"}}


class _TracerStreamDeepAgent(_FakeDeepAgent):
    async def stream(self, inputs: dict[str, Any], session: _FakeSession):
        """Yield Jiuwen tracer chunks around assistant output."""
        self.session = session
        yield {
            "type": "tracer_agent",
            "payload": {
                "traceId": "trace-1",
                "invokeId": "invoke-1",
                "name": "list_files",
                "status": "start",
                "inputs": {"inputs": {"path": "/tmp/workspace"}},
                "metaData": {"type": "tool", "class_name": "list_files"},
            },
        }
        yield {
            "type": "tracer_agent",
            "payload": {
                "traceId": "trace-1",
                "invokeId": "invoke-1",
                "name": "list_files",
                "status": "finish",
                "inputs": {"inputs": {"path": "/tmp/workspace"}},
                "outputs": {"outputs": {"success": True, "content": "a.csv"}},
                "metaData": {"type": "tool", "class_name": "list_files"},
            },
        }
        yield {"type": "llm_output", "index": 2, "payload": {"content": "done"}}


class _StructuredToolResultStreamDeepAgent(_FakeDeepAgent):
    async def stream(self, inputs: dict[str, Any], session: _FakeSession):
        """Yield structured tool results that only carry data fields."""
        self.session = session
        yield {
            "type": "tracer_agent",
            "payload": {
                "name": "list_files",
                "status": "finish",
                "outputs": {"outputs": {"success": True, "data": {"files": ["a.md"], "dirs": ["skills"]}}},
                "metaData": {"type": "tool", "class_name": "list_files"},
            },
        }
        yield {
            "type": "tracer_agent",
            "payload": {
                "name": "glob",
                "status": "finish",
                "outputs": {"outputs": {"success": True, "data": {"filenames": ["AGENT.md", "SOUL.md"]}}},
                "metaData": {"type": "tool", "class_name": "glob"},
            },
        }
        yield {
            "type": "tracer_agent",
            "payload": {
                "name": "skill_tool",
                "status": "finish",
                "outputs": {"outputs": {"success": True, "data": {"skill_content": "# Skill"}}},
                "metaData": {"type": "tool", "class_name": "skill_tool"},
            },
        }


@pytest.mark.asyncio
async def test_chat_binds_card_and_runs_non_stream_session_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created: list[_FakeSession] = []

    def fake_create_agent_session(*, session_id: str, card: Any):
        session = _FakeSession(session_id, card)
        created.append(session)
        return session

    @asynccontextmanager
    async def fake_lease(spec: CheckpointerSpec):
        yield

    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        fake_create_agent_session,
    )
    monkeypatch.setattr(agent_module, "checkpointer_lease", fake_lease)

    deep_agent = _FakeDeepAgent()
    agent = agent_module.DataAgent({"WORKSPACE": {"path": str(tmp_path)}})
    agent._deep_agent = deep_agent
    agent._checkpointer_spec = CheckpointerSpec(type="in_memory", conf={})

    result = await agent.chat("hello", checkpoint_id="checkpoint-1")

    assert created[0].card is deep_agent.card
    assert created[0].events == ["pre_run", "invoke", "post_run"]
    assert result["session_id"] == "checkpoint-1"
    assert result["checkpoint_id"] == "checkpoint-1"


@pytest.mark.asyncio
async def test_stream_returns_checkpoint_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_create_agent_session(*, session_id: str, card: Any):
        return _FakeSession(session_id, card)

    @asynccontextmanager
    async def fake_lease(spec: CheckpointerSpec):
        yield

    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        fake_create_agent_session,
    )
    monkeypatch.setattr(agent_module, "checkpointer_lease", fake_lease)

    agent = agent_module.DataAgent({"WORKSPACE": {"path": str(tmp_path)}})
    agent._deep_agent = _FakeDeepAgent()
    agent._checkpointer_spec = CheckpointerSpec(type="in_memory", conf={})

    items = [
        item
        async for item in agent.astream(
            initial_state={"user_query": "hello"},
            checkpoint_id="stream-checkpoint",
        )
    ]

    assert items[-1] == (
        "updates",
        {
            "messages": [{"output": "streamed"}],
            "complete": True,
            "session_id": "stream-checkpoint",
            "checkpoint_id": "stream-checkpoint",
        },
    )


@pytest.mark.asyncio
async def test_stream_extracts_content_from_jiuwen_schema_dict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Schema-like dict chunks should stream payload content, not raw dictionaries."""
    def fake_create_agent_session(*, session_id: str, card: Any):
        return _FakeSession(session_id, card)

    @asynccontextmanager
    async def fake_lease(spec: CheckpointerSpec):
        yield

    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        fake_create_agent_session,
    )
    monkeypatch.setattr(agent_module, "checkpointer_lease", fake_lease)

    agent = agent_module.DataAgent({"WORKSPACE": {"path": str(tmp_path)}})
    agent._deep_agent = _SchemaDictStreamDeepAgent()
    agent._checkpointer_spec = CheckpointerSpec(type="in_memory", conf={})

    items = [
        item
        async for item in agent.astream(
            initial_state={"user_query": "hello"},
            checkpoint_id="schema-dict-stream",
        )
    ]

    assert items[0] == ("custom", {"type": "llm_output", "index": 0, "payload": {"content": "hello"}})
    assert items[1] == (
        "updates",
        {
            "messages": [{"role": "assistant", "content": "hello"}],
            "final_answer": "hello",
            "complete": True,
            "session_id": "schema-dict-stream",
            "checkpoint_id": "schema-dict-stream",
        },
    )
    assert "llm_usage" not in str(items)


@pytest.mark.asyncio
async def test_stream_converts_jiuwen_tracer_chunks_to_tool_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Tracer chunks should become Jiuwen-style tool events and stay out of messages."""
    def fake_create_agent_session(*, session_id: str, card: Any):
        return _FakeSession(session_id, card)

    @asynccontextmanager
    async def fake_lease(spec: CheckpointerSpec):
        yield

    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        fake_create_agent_session,
    )
    monkeypatch.setattr(agent_module, "checkpointer_lease", fake_lease)

    agent = agent_module.DataAgent({"WORKSPACE": {"path": str(tmp_path)}})
    agent._deep_agent = _TracerStreamDeepAgent()
    agent._checkpointer_spec = CheckpointerSpec(type="in_memory", conf={})

    items = [
        item
        async for item in agent.astream(
            initial_state={"user_query": "list files"},
            checkpoint_id="tracer-stream",
        )
    ]

    assert items[0][1]["type"] == "tool_call"
    assert items[0][1]["payload"]["tool_args"] == {"path": "/tmp/workspace"}
    assert items[1][1]["type"] == "tool_result"
    assert items[1][1]["payload"]["tool_output"] == {"success": True, "content": "a.csv"}
    assert items[1][1]["payload"]["tool_result"] == "a.csv"
    assert items[2] == ("custom", {"type": "llm_output", "index": 2, "payload": {"content": "done"}})
    assert items[-1][1]["messages"] == [{"role": "assistant", "content": "done"}]
    assert "tracer_agent" not in str(items[-1])


@pytest.mark.asyncio
async def test_stream_extracts_structured_tool_result_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Structured Jiuwen ToolOutput.data should be displayed as tool_result text."""
    def fake_create_agent_session(*, session_id: str, card: Any):
        return _FakeSession(session_id, card)

    @asynccontextmanager
    async def fake_lease(spec: CheckpointerSpec):
        yield

    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        fake_create_agent_session,
    )
    monkeypatch.setattr(agent_module, "checkpointer_lease", fake_lease)

    agent = agent_module.DataAgent({"WORKSPACE": {"path": str(tmp_path)}})
    agent._deep_agent = _StructuredToolResultStreamDeepAgent()
    agent._checkpointer_spec = CheckpointerSpec(type="in_memory", conf={})

    items = [
        item
        async for item in agent.astream(
            initial_state={"user_query": "show tools"},
            checkpoint_id="structured-tools",
        )
    ]

    payloads = [item[1]["payload"] for item in items if item[0] == "custom"]
    assert payloads[0]["tool_result"] == "skills/\na.md"
    assert payloads[1]["tool_result"] == "AGENT.md\nSOUL.md"
    assert payloads[2]["tool_result"] == "# Skill"


@pytest.mark.asyncio
async def test_persistence_checkpointer_recovers_new_session_state(tmp_path: Path) -> None:
    from openjiuwen.core.session.agent import create_agent_session
    from openjiuwen.core.session.checkpointer.checkpointer import (
        CheckpointerConfig,
        CheckpointerFactory,
    )
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard

    spec = build_checkpointer_spec(
        {
            "CHECKPOINTER": {
                "type": "persistence",
                "conf": {
                    "db_type": "sqlite",
                    "db_path": str(tmp_path / "checkpoint"),
                },
            }
        },
        workspace_root=tmp_path,
    )
    card = AgentCard(id="checkpoint-test-agent", name="checkpoint-test-agent")

    async with checkpointer_lease(spec):
        first = create_agent_session(session_id="persistent-session", card=card)
        await first.pre_run(inputs={"query": "first"})
        first.update_state({"saved_value": "restored"})
        await first.commit()

        restarted_checkpointer = await CheckpointerFactory.create(
            CheckpointerConfig(type=spec.type, conf=dict(spec.conf or {}))
        )
        CheckpointerFactory.set_default_checkpointer(restarted_checkpointer)

        restored = create_agent_session(session_id="persistent-session", card=card)
        await restored.pre_run()

        assert restored.get_state("saved_value") == "restored"
