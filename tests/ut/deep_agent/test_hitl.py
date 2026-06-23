# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""HITL YAML adapter and public resume protocol tests."""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dataagent.core.deep_agent.adapter import DeepAgentAdapter
from dataagent.core.deep_agent.builders.hitl import build_interactive_input
from dataagent.core.deep_agent.checkpoint import CheckpointerSpec
from dataagent.core.deep_agent.spec import DeepAgentBuildSpec
from dataagent.interface.sdk import agent as agent_module

cli_module = importlib.import_module("dataagent.interface.cli.main")


def _interaction_schema(interrupt_id: str = "call-1") -> Any:
    from openjiuwen.core.session.stream import OutputSchema

    return OutputSchema(
        type="__interaction__",
        index=0,
        payload={
            "id": interrupt_id,
            "value": {
                "tool_name": "ask_user",
                "tool_call_id": interrupt_id,
                "questions": [
                    {
                        "question": "Which plan?",
                        "header": "Plan",
                        "options": [{"label": "A"}, {"label": "B"}],
                    }
                ],
                "payload_schema": {"type": "object"},
            },
        },
    )


def test_human_feedback_yaml_switch_defaults_to_disabled() -> None:
    assert DeepAgentBuildSpec.from_config({}).enable_human_feedback is False
    assert (
        DeepAgentBuildSpec.from_config(
            {"AGENT_CONFIG": {"enable_human_feedback": True}}
        ).enable_human_feedback
        is True
    )
    assert DeepAgentBuildSpec.from_config({"enable_human_feedback": True}).enable_human_feedback is True
    assert (
        DeepAgentBuildSpec.from_config(
            {
                "enable_human_feedback": True,
                "AGENT_CONFIG": {"enable_human_feedback": False},
            }
        ).enable_human_feedback
        is True
    )


def test_human_feedback_yaml_switch_requires_boolean() -> None:
    with pytest.raises(ValueError, match="must be a boolean"):
        DeepAgentBuildSpec.from_config(
            {"AGENT_CONFIG": {"enable_human_feedback": "true"}}
        )


def test_adapter_builds_ask_user_rail_only_when_enabled() -> None:
    from openjiuwen.harness.rails import AskUserRail

    assert DeepAgentAdapter({}).build_hitl_rail() is None
    rail = DeepAgentAdapter(
        {"AGENT_CONFIG": {"enable_human_feedback": True}}
    ).build_hitl_rail()

    assert isinstance(rail, AskUserRail)


def test_build_interactive_input_supports_single_and_batch_responses() -> None:
    single = build_interactive_input("Plan B", interrupt_id="call-1")
    assert single.user_inputs == {"call-1": "Plan B"}

    structured = build_interactive_input(
        {"interrupt_id": "call-2", "answers": {"Which plan?": "Plan A"}}
    )
    assert structured.user_inputs == {
        "call-2": {"answers": {"Which plan?": "Plan A"}}
    }

    batch = build_interactive_input(
        {
            "responses": [
                {"interrupt_id": "call-1", "answer": "yes"},
                {"interrupt_id": "call-2", "payload": {"approved": False}},
            ]
        }
    )
    assert batch.user_inputs == {
        "call-1": {"answer": "yes"},
        "call-2": {"approved": False},
    }


def test_build_interactive_input_requires_interrupt_identity() -> None:
    with pytest.raises(ValueError, match="interrupt_id is required"):
        build_interactive_input("orphaned answer")


class _FakeSession:
    def __init__(self, session_id: str, card: Any) -> None:
        self.session_id = session_id
        self.card = card

    async def pre_run(self, **kwargs: Any) -> None:
        return None

    async def post_run(self) -> None:
        return None


class _InterruptDeepAgent:
    def __init__(self, *, stream: bool = False) -> None:
        self.card = SimpleNamespace(id="hitl-agent", name="hitl-agent")
        self.inputs: dict[str, Any] | None = None
        self._stream = stream

    async def invoke(self, inputs: dict[str, Any], session: _FakeSession) -> dict[str, Any]:
        self.inputs = inputs
        return {
            "result_type": "interrupt",
            "interrupt_ids": ["call-1"],
            "state": [_interaction_schema()],
        }

    async def stream(self, inputs: dict[str, Any], session: _FakeSession):
        self.inputs = inputs
        yield _interaction_schema()


def _install_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
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


@pytest.mark.asyncio
async def test_chat_returns_serializable_interrupt_and_accepts_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_runtime(monkeypatch)
    deep_agent = _InterruptDeepAgent()
    agent = agent_module.DataAgent({"WORKSPACE": {"path": str(tmp_path)}})
    agent._deep_agent = deep_agent
    agent._checkpointer_spec = CheckpointerSpec(type="in_memory", conf={})

    result = await agent.chat("choose", checkpoint_id="hitl-session")

    assert result["interrupted"] is True
    assert result["complete"] is False
    assert result["interrupt_ids"] == ["call-1"]
    assert result["interrupts"][0]["questions"][0]["question"] == "Which plan?"
    assert isinstance(result["state"], list)

    await agent.chat(
        "",
        checkpoint_id="hitl-session",
        human_feedback={"interrupt_id": "call-1", "answer": "Plan B"},
    )

    assert deep_agent.inputs is not None
    assert deep_agent.inputs["query"].user_inputs == {
        "call-1": {"answer": "Plan B"}
    }


@pytest.mark.asyncio
async def test_stream_propagates_structured_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_runtime(monkeypatch)
    agent = agent_module.DataAgent({"WORKSPACE": {"path": str(tmp_path)}})
    agent._deep_agent = _InterruptDeepAgent(stream=True)
    agent._checkpointer_spec = CheckpointerSpec(type="in_memory", conf={})

    items = [
        item
        async for item in agent.astream(
            initial_state={"user_query": "choose"},
            checkpoint_id="hitl-stream",
        )
    ]

    assert items[0][0] == "custom"
    assert items[0][1]["type"] == "interaction"
    assert items[0][1]["interrupt_id"] == "call-1"
    assert items[-1][0] == "updates"
    final_update = items[-1][1]
    assert final_update["result_type"] == "interrupt"
    assert final_update["interrupted"] is True
    assert final_update["interrupt_ids"] == ["call-1"]
    assert final_update["interrupts"][0]["questions"][0]["question"] == "Which plan?"
    assert final_update["complete"] is False
    assert final_update["session_id"] == "hitl-stream"
    assert final_update["checkpoint_id"] == "hitl-stream"


@pytest.mark.asyncio
async def test_cli_collects_multi_question_feedback(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["Plan B", "Tomorrow"])
    monkeypatch.setattr(cli_module.Prompt, "ask", lambda prompt: next(answers))

    feedback = await cli_module._prompt_for_human_feedback(
        {
            "interrupts": [
                {
                    "interrupt_id": "call-1",
                    "questions": [
                        {"question": "Which plan?", "header": "Plan"},
                        {"question": "When?", "header": "Schedule"},
                    ],
                }
            ]
        }
    )

    assert feedback == {
        "responses": [
            {
                "interrupt_id": "call-1",
                "answers": {"Which plan?": "Plan B", "When?": "Tomorrow"},
            }
        ]
    }
