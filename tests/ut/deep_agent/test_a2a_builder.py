# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""YAML-to-OpenJiuWen A2A adapter tests."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from a2a.types import AgentCapabilities
from a2a.types import AgentCard as RemoteAgentCard

from dataagent.core.deep_agent.adapter import DeepAgentAdapter
from dataagent.core.deep_agent.builders.tools.a2a import (
    A2AAgentBinding,
    build_a2a_agents,
    register_a2a_agents,
    unregister_a2a_agents,
)
from dataagent.core.deep_agent.spec import A2AAgentSpec, DeepAgentBuildSpec


def _remote_card(name: str = "discovered-agent") -> RemoteAgentCard:
    return RemoteAgentCard(
        name=name,
        description="Discovered remote agent.",
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )


def _spec(**overrides) -> A2AAgentSpec:
    values = {
        "path": "TOOLS.A2A[0]",
        "agent_id": "remote-id",
        "name": "remote-alias",
        "url": "http://localhost:9000",
    }
    values.update(overrides)
    return A2AAgentSpec(**values)


def test_normalizes_flat_and_named_a2a_yaml() -> None:
    spec = DeepAgentBuildSpec.from_config(
        {
            "TOOLS": {
                "A2A": [
                    {
                        "name": "flat-agent",
                        "agent_id": "flat-id",
                        "url": "http://localhost:9000/a2a",
                    }
                ],
                "a2a": [
                    {
                        "mapped-agent": {
                            "base_url": "https://example.test/root/a2a/jsonrpc/",
                            "description": "Mapped remote agent.",
                        }
                    }
                ],
            }
        }
    )

    assert spec.a2a_agents == (
        A2AAgentSpec(
            path="TOOLS.A2A[0]",
            agent_id="flat-id",
            name="flat-agent",
            url="http://localhost:9000",
        ),
        A2AAgentSpec(
            path="TOOLS.a2a[0]",
            agent_id="mapped-agent",
            name="mapped-agent",
            url="https://example.test/root",
            description="Mapped remote agent.",
        ),
    )


def test_url_only_a2a_yaml_uses_remote_card_identity() -> None:
    spec = DeepAgentBuildSpec.from_config(
        {
            "TOOLS": {
                "A2A": [
                    {
                        "url": "https://example.test",
                    }
                ]
            }
        }
    )

    assert spec.a2a_agents == (
        A2AAgentSpec(
            path="TOOLS.A2A[0]",
            agent_id=None,
            name=None,
            url="https://example.test",
        ),
    )


def test_auth_and_timeout_are_limited_to_discovery() -> None:
    spec = DeepAgentBuildSpec.from_config(
        {
            "TOOLS": {
                "A2A": [
                    {
                        "url": "https://example.test",
                        "auth_token": "secret",
                        "timeout": 5,
                    }
                ]
            }
        }
    )

    assert spec.a2a_agents[0].auth_token == "secret"
    assert spec.a2a_agents[0].discovery_timeout == 5.0
    assert "discovery only" in spec.diagnostics[0]
    assert "does not forward" in spec.diagnostics[1]


def test_builds_remote_agent_from_discovered_card(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "dataagent.core.deep_agent.builders.tools.a2a._discover_remote_agent_card",
        lambda spec: _remote_card(),
    )

    binding = build_a2a_agents([_spec(agent_id=None, name=None)])[0]

    assert binding.card.id == "discovered-agent"
    assert binding.card.name == "discovered-agent"
    assert binding.card.description == "Discovered remote agent."
    assert binding.card.input_params["required"] == ["query"]
    assert binding.remote.agent_id == "discovered-agent"
    assert binding.remote.config.url == "http://localhost:9000"
    assert binding.remote.config.kwargs["card"] is binding.card
    assert binding.remote.card is binding.card


def test_empty_a2a_config_builds_nothing() -> None:
    assert build_a2a_agents([]) == []


def test_yaml_alias_overrides_discovered_card_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "dataagent.core.deep_agent.builders.tools.a2a._discover_remote_agent_card",
        lambda spec: _remote_card(),
    )

    binding = build_a2a_agents([_spec()])[0]

    assert binding.card.id == "remote-id"
    assert binding.card.name == "remote-alias"
    assert binding.card.description == "Discovered remote agent."


def test_discovery_uses_standard_card_endpoint_and_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {
                "name": "remote",
                "description": "Remote agent",
                "capabilities": {"streaming": True},
                "defaultInputModes": ["text/plain"],
                "defaultOutputModes": ["text/plain"],
            }

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("httpx.get", fake_get)
    binding = build_a2a_agents(
        [
            _spec(
                agent_id=None,
                name=None,
                auth_token="token",
                discovery_timeout=3.0,
            )
        ]
    )[0]

    assert captured["url"] == "http://localhost:9000/.well-known/agent-card.json"
    assert captured["headers"] == {"Authorization": "Bearer token"}
    assert captured["timeout"] == 3.0
    assert binding.card.name == "remote"


def test_discovery_failure_reports_yaml_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_get(url, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr("httpx.get", fail_get)

    with pytest.raises(ValueError, match="TOOLS.A2A\\[0\\].*well-known.*offline"):
        build_a2a_agents([_spec()])


def test_registers_and_unregisters_remote_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    from openjiuwen.core.runner.runner import Runner
    from openjiuwen.core.single_agent import AbilityManager
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard

    monkeypatch.setattr(
        "dataagent.core.deep_agent.builders.tools.a2a._discover_remote_agent_card",
        lambda spec: _remote_card(),
    )
    unique_id = f"a2a-{uuid.uuid4().hex}"
    binding = build_a2a_agents([_spec(agent_id=unique_id, name=unique_id)])[0]
    deep_agent = SimpleNamespace(
        card=AgentCard(id=f"parent-{uuid.uuid4().hex}", name="parent"),
        ability_manager=AbilityManager(),
    )

    try:
        registered = register_a2a_agents([binding], deep_agent)
        assert registered == [binding]
        assert deep_agent.ability_manager.get(unique_id) is binding.card
    finally:
        unregister_a2a_agents([binding], deep_agent=deep_agent)

    assert deep_agent.ability_manager.get(unique_id) is None


@pytest.mark.asyncio
async def test_agent_ability_invokes_remote_with_child_conversation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openjiuwen.core.foundation.llm import ToolCall
    from openjiuwen.core.runner.runner import Runner
    from openjiuwen.core.session.agent import create_agent_session
    from openjiuwen.core.single_agent import AbilityManager
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.core.single_agent.schema.agent_result import AgentResult

    monkeypatch.setattr(
        "dataagent.core.deep_agent.builders.tools.a2a._discover_remote_agent_card",
        lambda spec: _remote_card(),
    )
    unique_id = f"a2a-{uuid.uuid4().hex}"
    binding = build_a2a_agents([_spec(agent_id=unique_id, name=unique_id)])[0]
    binding.remote.invoke = AsyncMock(return_value=AgentResult())
    ability_manager = AbilityManager()
    deep_agent = SimpleNamespace(
        card=AgentCard(id=f"parent-{uuid.uuid4().hex}", name="parent"),
        ability_manager=ability_manager,
    )
    session = create_agent_session(
        session_id="parent-session",
        card=deep_agent.card,
    )
    tool_call = ToolCall(
        id="call-1",
        type="function",
        name=unique_id,
        arguments='{"query": "analyze this"}',
    )

    try:
        register_a2a_agents([binding], deep_agent)
        result, _ = await ability_manager._execute_single_tool_call(tool_call, session)
        assert isinstance(result, AgentResult)
        binding.remote.invoke.assert_awaited_once()
        inputs = binding.remote.invoke.await_args.args[0]
        assert inputs["query"] == "analyze this"
        assert inputs["conversation_id"] == "parent-session:call-1"
        assert await Runner.resource_mgr.get_agent(unique_id) is binding.remote
    finally:
        unregister_a2a_agents([binding], deep_agent=deep_agent)


def test_registration_rolls_back_on_ability_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    from openjiuwen.core.runner.runner import Runner
    from openjiuwen.core.single_agent import AbilityManager
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard

    monkeypatch.setattr(
        "dataagent.core.deep_agent.builders.tools.a2a._discover_remote_agent_card",
        lambda spec: _remote_card(),
    )
    first_id = f"a2a-{uuid.uuid4().hex}"
    second_id = f"a2a-{uuid.uuid4().hex}"
    first, second = build_a2a_agents(
        [
            _spec(path="TOOLS.A2A[0]", agent_id=first_id, name=first_id),
            _spec(path="TOOLS.A2A[1]", agent_id=second_id, name=second_id),
        ]
    )
    ability_manager = AbilityManager()
    ability_manager.add(AgentCard(id="existing", name=second_id))
    deep_agent = SimpleNamespace(
        card=AgentCard(id=f"parent-{uuid.uuid4().hex}", name="parent"),
        ability_manager=ability_manager,
    )

    with pytest.raises(ValueError, match="conflicts with an existing"):
        register_a2a_agents([first, second], deep_agent)

    assert Runner.resource_mgr.get_resource_tag(first_id) is None


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"TOOLS": {"A2A": "bad"}}, "must be a list"),
        ({"TOOLS": {"A2A": [{}]}}, "requires url or base_url"),
        ({"TOOLS": {"A2A": [{"url": "relative/path"}]}}, "absolute http"),
        ({"TOOLS": {"A2A": [{"url": "https://example.test?tenant=1"}]}}, "must not contain"),
        ({"TOOLS": {"A2A": [{"url": "https://example.test", "timeout": 0}]}}, "positive number"),
        (
            {
                "TOOLS": {
                    "A2A": [
                        {"name": "duplicate", "url": "https://one.test"},
                        {"name": "duplicate", "url": "https://two.test"},
                    ]
                }
            },
            "duplicates A2A",
        ),
    ],
)
def test_rejects_invalid_a2a_yaml(config: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message) as exc_info:
        DeepAgentBuildSpec.from_config(config)

    assert "TOOLS.A2A" in str(exc_info.value)


def test_adapter_builds_a2a_bindings(monkeypatch: pytest.MonkeyPatch) -> None:
    binding = object()
    monkeypatch.setattr(
        "dataagent.core.deep_agent.adapter.build_a2a_agents",
        lambda specs: [binding],
    )

    adapter = DeepAgentAdapter({"TOOLS": {"A2A": [{"url": "https://example.test"}]}})

    assert adapter.build_a2a_agents() == [binding]


@pytest.mark.asyncio
async def test_dataagent_aclose_unregisters_and_stops_a2a(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataagent.interface.sdk.agent import DataAgent

    client = SimpleNamespace(
        is_started=lambda: True,
        stop=AsyncMock(),
    )
    binding = SimpleNamespace(
        card=SimpleNamespace(id="remote", name="remote"),
        remote=SimpleNamespace(client=client),
    )
    unregister_calls = []

    monkeypatch.setattr(
        DeepAgentAdapter,
        "unregister_a2a_agents",
        staticmethod(lambda bindings, deep_agent=None: unregister_calls.append((bindings, deep_agent))),
    )

    agent = DataAgent({"TOOLS": {}})
    agent._deep_agent = object()
    agent._a2a_agents = [binding]

    await agent.aclose()

    assert unregister_calls == [([binding], agent._deep_agent)]
    client.stop.assert_awaited_once()
    assert agent._a2a_agents == []


@pytest.mark.asyncio
async def test_stop_closes_never_invoked_sdk_client() -> None:
    from dataagent.core.deep_agent.builders.tools.a2a import stop_a2a_agents

    sdk_client = SimpleNamespace(stop=AsyncMock())
    remote_client = SimpleNamespace(
        is_started=lambda: False,
        client=sdk_client,
    )
    binding = SimpleNamespace(remote=SimpleNamespace(client=remote_client))

    await stop_a2a_agents([binding])

    sdk_client.stop.assert_awaited_once()


def test_dataagent_registers_a2a_after_deep_agent_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import openjiuwen.harness
    from openjiuwen.core.runner.runner import Runner

    from dataagent.interface.sdk import agent as agent_module

    deep_agent = object()
    binding = object()
    sys_operation = object()
    calls = []

    class AddResult:
        @staticmethod
        def is_err() -> bool:
            return False

    monkeypatch.setattr(agent_module, "build_model_from_config", lambda config: object())
    monkeypatch.setattr(agent_module, "build_system_prompt", lambda config: "prompt")
    monkeypatch.setattr(agent_module.DataAgent, "_resolve_workspace", lambda self: tmp_path)
    monkeypatch.setattr(
        "dataagent.core.deep_agent.adapter.DeepAgentAdapter.build_tools",
        lambda self, operation, **kwargs: [],
    )
    monkeypatch.setattr(
        "dataagent.core.deep_agent.adapter.DeepAgentAdapter.build_mcps",
        lambda self: [],
    )
    monkeypatch.setattr(
        "dataagent.core.deep_agent.adapter.DeepAgentAdapter.build_a2a_agents",
        lambda self: [binding],
    )

    def fake_register(self, bindings, created_agent):
        calls.append(("register", bindings, created_agent))
        return bindings

    monkeypatch.setattr(
        "dataagent.core.deep_agent.adapter.DeepAgentAdapter.register_a2a_agents",
        fake_register,
    )
    monkeypatch.setattr(Runner.resource_mgr, "add_sys_operation", lambda card: AddResult())
    monkeypatch.setattr(Runner.resource_mgr, "get_sys_operation", lambda resource_id: sys_operation)

    def fake_create_deep_agent(**kwargs):
        calls.append(("create", kwargs))
        return deep_agent

    monkeypatch.setattr(openjiuwen.harness, "create_deep_agent", fake_create_deep_agent)

    agent = agent_module.DataAgent(
        {
            "AGENT_CONFIG": {"name": "a2a-test"},
            "TOOLS": {"A2A": [{"url": "https://example.test"}]},
        }
    )
    agent._build_deep_agent()

    assert calls[0][0] == "create"
    assert calls[1] == ("register", [binding], deep_agent)
    assert "tools" in calls[0][1]
    assert "mcps" in calls[0][1]
    assert agent._deep_agent is deep_agent
    assert agent._a2a_agents == [binding]
