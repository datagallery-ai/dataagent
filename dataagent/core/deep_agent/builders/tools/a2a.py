# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Build and register OpenJiuWen A2A RemoteAgent abilities."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from dataagent.core.deep_agent.spec import A2AAgentSpec

if TYPE_CHECKING:
    from openjiuwen.core.runner.drunner.remote_client.remote_agent import RemoteAgent
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.harness import DeepAgent


@dataclass(frozen=True)
class A2AAgentBinding:
    """A Jiuwen AgentCard paired with its A2A RemoteAgent provider."""

    path: str
    card: AgentCard
    remote: RemoteAgent


def build_a2a_agents(specs: Iterable[A2AAgentSpec]) -> list[A2AAgentBinding]:
    """Discover remote cards and build Jiuwen RemoteAgents."""
    normalized_specs = list(specs)
    if not normalized_specs:
        return []

    from openjiuwen.core.runner.drunner.remote_client.remote_agent import RemoteAgent
    from openjiuwen.core.runner.drunner.remote_client.remote_client_config import ProtocolEnum
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.extensions.a2a.a2a_agentcard_adapter import A2AAgentCardAdapter

    bindings: list[A2AAgentBinding] = []
    for spec in normalized_specs:
        remote_card = _discover_remote_agent_card(spec)
        discovered_card = A2AAgentCardAdapter.from_a2a_agent_card(remote_card)
        name = spec.name or discovered_card.name
        if not name:
            raise ValueError(f"{spec.path} remote AgentCard does not provide a usable name")
        agent_id = spec.agent_id or name
        card = AgentCard(
            id=agent_id,
            name=name,
            description=spec.description or discovered_card.description or f"Remote A2A agent {name}.",
            input_params={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The task or question to send to the remote agent.",
                    }
                },
                "required": ["query"],
                "additionalProperties": True,
            },
        )
        remote = RemoteAgent(
            agent_id=agent_id,
            description=card.description,
            protocol=ProtocolEnum.A2A,
            config={
                "url": spec.url,
                "kwargs": {"card": card},
            },
        )
        # AbilityManager creates the child session from provider.card.
        remote.card = card
        bindings.append(A2AAgentBinding(path=spec.path, card=card, remote=remote))
    return bindings


def _discover_remote_agent_card(spec: A2AAgentSpec):
    import httpx
    from a2a.client.card_resolver import parse_agent_card

    headers = {"Authorization": f"Bearer {spec.auth_token}"} if spec.auth_token else {}
    target_url = f"{spec.url}/.well-known/agent-card.json"
    try:
        response = httpx.get(
            target_url,
            headers=headers,
            timeout=spec.discovery_timeout,
        )
        response.raise_for_status()
        return parse_agent_card(response.json())
    except Exception as exc:
        raise ValueError(f"{spec.path} failed to discover AgentCard from {target_url}: {exc}") from exc


def register_a2a_agents(
    bindings: Iterable[A2AAgentBinding],
    deep_agent: DeepAgent,
) -> list[A2AAgentBinding]:
    """Register RemoteAgents and expose their cards to a DeepAgent."""
    from openjiuwen.core.runner.runner import Runner

    registered: list[A2AAgentBinding] = []
    try:
        for binding in bindings:
            existing = deep_agent.ability_manager.get(binding.card.name)
            if existing is not None:
                raise ValueError(
                    f"{binding.path}.name {binding.card.name!r} conflicts with an existing DeepAgent ability"
                )

            result = Runner.resource_mgr.add_agent(
                binding.card,
                binding.remote,
                tag=deep_agent.card.id,
            )
            if result.is_err():
                raise ValueError(
                    f"{binding.path} failed to register A2A agent {binding.card.id!r}: {result.msg()}"
                )

            add_result = deep_agent.ability_manager.add(binding.card)
            if not add_result.added:
                Runner.resource_mgr.remove_agent(
                    agent_id=binding.card.id,
                    tag=deep_agent.card.id,
                    skip_if_tag_not_exists=True,
                )
                raise ValueError(
                    f"{binding.path}.name {binding.card.name!r} was not added to DeepAgent: {add_result.reason}"
                )
            registered.append(binding)
    except Exception:
        unregister_a2a_agents(registered, deep_agent=deep_agent)
        raise

    return registered


def unregister_a2a_agents(
    bindings: Iterable[A2AAgentBinding],
    *,
    deep_agent: DeepAgent | None = None,
) -> None:
    """Detach A2A abilities and remove their Runner resources."""
    from openjiuwen.core.runner.runner import Runner

    for binding in reversed(list(bindings)):
        if deep_agent is not None:
            deep_agent.ability_manager.remove(binding.card.name)
        Runner.resource_mgr.remove_agent(
            agent_id=binding.card.id,
            tag=getattr(getattr(deep_agent, "card", None), "id", "__global__"),
            skip_if_tag_not_exists=True,
        )


async def stop_a2a_agents(bindings: Iterable[A2AAgentBinding]) -> None:
    """Close both started and never-invoked A2A clients."""
    for binding in bindings:
        client: Any = binding.remote.client
        if client.is_started():
            await client.stop()
            continue

        sdk_client = getattr(client, "client", None)
        stop = getattr(sdk_client, "stop", None)
        if callable(stop):
            await stop()
