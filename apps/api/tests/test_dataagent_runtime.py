from __future__ import annotations

from pathlib import Path

import pytest
from ag_ui.core.types import RunAgentInput
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.store.memory import InMemoryStore

from datafoundry_api.agent import AgentScopeError, DataAgentRuntime, ScopedLangGraphAgent
from datafoundry_api.model_profiles import RuntimeModelSelection


def _config_path() -> Path:
    return Path(__file__).parents[1] / "src" / "datafoundry_api" / "default_dataagent.yaml"


@pytest.mark.asyncio
async def test_runtime_caches_graph_by_user_and_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai-compatible")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-api-key")
    checkpointer = InMemorySaver()
    store = InMemoryStore()
    runtime = DataAgentRuntime(_config_path(), checkpointer=checkpointer, store=store)

    first = await runtime.agent_for("user-a", "thread-1")
    second = await runtime.agent_for("user-a", "thread-1")
    other_user = await runtime.agent_for("user-b", "thread-1")

    assert first is not second
    assert first.graph is second.graph
    assert first.graph is not other_user.graph
    assert first.graph.checkpointer is checkpointer
    assert first.graph.store is store


@pytest.mark.asyncio
async def test_runtime_rebuilds_graph_for_model_profile_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai-compatible")
    monkeypatch.setenv("LLM_MODEL", "server-model")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("LLM_API_KEY", "server-api-key")
    runtime = DataAgentRuntime(
        _config_path(),
        checkpointer=InMemorySaver(),
        store=InMemoryStore(),
    )
    model_slot = {
        "chat_model": {
            "provider": "openai-compatible",
            "model_type": "chat",
            "params": {
                "model": "profile-model",
                "base_url": "http://127.0.0.1:9/v1",
                "api_key": "profile-api-key",
            },
        }
    }
    revision_one = RuntimeModelSelection(cache_key="profile:1", model_slots=model_slot)
    revision_two = RuntimeModelSelection(cache_key="profile:2", model_slots=model_slot)

    first = await runtime.agent_for("user-a", "thread-1", revision_one)
    cached = await runtime.agent_for("user-a", "thread-1", revision_one)
    rebuilt = await runtime.agent_for("user-a", "thread-1", revision_two)

    assert first.graph is cached.graph
    assert first.graph is not rebuilt.graph


@pytest.mark.asyncio
async def test_runtime_rejects_unsafe_thread_id() -> None:
    runtime = DataAgentRuntime(
        _config_path(),
        checkpointer=InMemorySaver(),
        store=InMemoryStore(),
    )

    with pytest.raises(AgentScopeError, match="session_id"):
        await runtime.agent_for("user-a", "../other-user")


@pytest.mark.asyncio
async def test_scoped_agent_separates_checkpoints_and_restores_public_thread_id() -> None:
    saver = InMemorySaver()
    builder = StateGraph(MessagesState)

    async def respond(_state: MessagesState) -> dict[str, list[AIMessage]]:
        return {"messages": [AIMessage(content="ok")]}

    builder.add_node("respond", respond)
    builder.add_edge(START, "respond")
    builder.add_edge("respond", END)
    graph = builder.compile(checkpointer=saver)
    public_thread_ids: set[str] = set()

    for user_id in ("user-a", "user-b"):
        agent = ScopedLangGraphAgent(user_id=user_id, name="test", graph=graph)
        run_input = RunAgentInput.model_validate(
            {
                "threadId": "shared-thread",
                "runId": f"run-{user_id}",
                "messages": [{"id": f"message-{user_id}", "role": "user", "content": user_id}],
                "tools": [],
                "context": [],
                "forwardedProps": {},
            }
        )
        async for event in agent.run(run_input):
            event_thread_id = getattr(event, "thread_id", None)
            if event_thread_id:
                public_thread_ids.add(event_thread_id)

    stored_thread_ids = {item.config.get("configurable", {}).get("thread_id") async for item in saver.alist(None)}
    assert public_thread_ids == {"shared-thread"}
    assert "datafoundry:user:user-a:thread:shared-thread" in stored_thread_ids
    assert "datafoundry:user:user-b:thread:shared-thread" in stored_thread_ids
