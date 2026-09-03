"""Bind the repository DataAgent runtime to the AG-UI transport."""

from __future__ import annotations

from asyncio import Lock
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from ag_ui_langgraph import LangGraphAgent
from dataagent import DataAgent
from dataagent.utils.runtime_paths import validate_session_id, validate_user_id
from langgraph.store.base import BaseStore
from langgraph.types import Checkpointer

from datafoundry_api.model_profiles import RuntimeModelSelection


class AgentScopeError(ValueError):
    """Raised when a client-provided user or thread identifier is unsafe."""


class ScopedLangGraphAgent(LangGraphAgent):
    """Keep the public AG-UI thread id while scoping checkpoint ids by user."""

    def __init__(self, *, user_id: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._user_id = user_id

    async def run(self, input: Any) -> AsyncIterator[Any]:
        """Run with a user-scoped checkpoint thread and restore public event ids."""
        client_thread_id = input.thread_id
        scoped_thread_id = f"datafoundry:user:{self._user_id}:thread:{client_thread_id}"
        scoped_input = input.model_copy(update={"thread_id": scoped_thread_id})
        async for event in super().run(scoped_input):
            if getattr(event, "thread_id", None) == scoped_thread_id and hasattr(event, "model_copy"):
                event = event.model_copy(update={"thread_id": client_thread_id})
            yield event


class DataAgentRuntime:
    """Create isolated DataAgent graphs for authenticated users and threads."""

    def __init__(
        self,
        config_path: Path,
        *,
        checkpointer: Checkpointer,
        store: BaseStore,
    ) -> None:
        self._config_path = config_path.expanduser().resolve()
        self._checkpointer = checkpointer
        self._store = store
        self._data_agent: DataAgent | None = None
        self._profile_agents: dict[str, DataAgent] = {}
        self._lock = Lock()

    async def agent_for(
        self,
        user_id: str,
        thread_id: str,
        model_selection: RuntimeModelSelection | None = None,
    ) -> LangGraphAgent:
        """Return a fresh AG-UI adapter backed by the user's cached thread graph."""
        try:
            resolved_user_id = validate_user_id(user_id)
            resolved_thread_id = validate_session_id(thread_id)
        except ValueError as exc:
            raise AgentScopeError(str(exc)) from exc
        data_agent = await self._data_agent_for_model(resolved_user_id, model_selection)
        graph = await data_agent.build_agent_graph(
            user_id=resolved_user_id,
            session_id=resolved_thread_id,
        )
        return ScopedLangGraphAgent(
            user_id=resolved_user_id,
            name="dataFoundry",
            description=data_agent.description(),
            graph=graph,
        )

    async def _data_agent_for_model(
        self,
        user_id: str,
        selection: RuntimeModelSelection | None,
    ) -> DataAgent:
        base_agent = await self._load_data_agent()
        if selection is None or selection.model_slots is None:
            return base_agent
        cache_key = f"{user_id}:{selection.cache_key}"
        async with self._lock:
            cached = self._profile_agents.get(cache_key)
            if cached is not None:
                return cached
            config = base_agent.config.copy()
            model_slots = {
                name: dict(slot) if isinstance(slot, Mapping) else slot for name, slot in selection.model_slots.items()
            }
            config.set("MODEL", model_slots)
            config.set("AGENT_CONFIG.primary_model", selection.primary_model_name)
            configured = DataAgent(config, checkpointer=self._checkpointer, store=self._store)
            self._profile_agents[cache_key] = configured
            return configured

    async def _load_data_agent(self) -> DataAgent:
        if self._data_agent is None:
            async with self._lock:
                if self._data_agent is None:
                    self._data_agent = DataAgent.from_config(
                        self._config_path,
                        checkpointer=self._checkpointer,
                        store=self._store,
                    )
        return self._data_agent
