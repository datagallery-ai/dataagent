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
"""Public SDK for the native Deep Agents based DataAgent runtime."""

from __future__ import annotations

import os
import uuid
from asyncio import Lock
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.types import Checkpointer

from dataagent.agents.nl2sql.agent import create_nl2sql_agent
from dataagent.common_utils.outbound_tls import ENV_PRESERVE_ON_MISSING, apply_certificate_config
from dataagent.config import ConfigManager
from dataagent.core.deepagents import DeepAgentConfigCompiler, create_data_agent
from dataagent.core.deepagents.config.models import ModelConfigCompiler
from dataagent.core.deepagents.config.workspace import WorkspaceConfigCompiler
from dataagent.core.errors import DataAgentError
from dataagent.utils.log import logger
from dataagent.utils.runtime_paths import validate_session_id, validate_user_id


class DataAgent:
    """Load legacy YAML and expose a native Deep Agent through the stable SDK."""

    def __init__(
        self,
        config: Any,
        *,
        checkpointer: Checkpointer | bool | None = None,
        store: BaseStore | None = None,
    ) -> None:
        self.config = config.copy()
        self.backend = "langgraph"
        self.type = str(config.get("AGENT_CONFIG.type", "react") or "react").strip().lower()
        self.agent_type = self.type
        self._checkpointer = checkpointer
        self._store = store
        self._chat_agent_instances: dict[tuple[str, str], CompiledStateGraph] = {}
        self._chat_agent_lock = Lock()
        logger.trace("DataAgent initialized with native Deep Agents runtime")

    def __repr__(self) -> str:
        return f"DataAgent(backend={self.backend}, config_loaded={bool(self.config)})"

    @classmethod
    def from_config(
        cls,
        config: str | Path,
        *,
        checkpointer: Checkpointer | bool | None = None,
        store: BaseStore | None = None,
    ) -> DataAgent:
        """Create a DataAgent from an existing YAML configuration file."""
        config_manager = ConfigManager()
        config_manager.reload(str(config), default_config_path=None)
        apply_certificate_config(
            config_manager.get("certificate"),
            preserve_existing_on_missing=os.getenv(ENV_PRESERVE_ON_MISSING) == "1",
        )
        return cls(config=config_manager, checkpointer=checkpointer, store=store)

    def astream(
        self,
        input: Any = None,
        *,
        initial_state: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        checkpoint_id: str | None = None,
        stream_mode: Any = "values",
        config: RunnableConfig | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """Stream native LangGraph events without converting their protocol."""
        graph_input = self._prepare_stream_input(input, initial_state)
        session_state = initial_state if initial_state is not None else input if isinstance(input, Mapping) else None
        resolved_session_id = self._resolve_session_id(session_id, session_state)
        resolved_user_id = self._resolve_user_id(session_state)
        run_config = self._build_run_config(resolved_user_id, resolved_session_id, checkpoint_id, config)

        async def _stream() -> AsyncIterator[Any]:
            try:
                chat_agent = await self._get_chat_agent(resolved_user_id, resolved_session_id)
                async for item in chat_agent.astream(
                    graph_input,
                    config=run_config,
                    stream_mode=stream_mode,
                    **kwargs,
                ):
                    yield item
            except DataAgentError:
                raise
            except Exception as exc:
                raise DataAgentError.from_exception(exc, component="sdk") from exc

        return _stream()

    async def select_engine(
        self,
        config: Any,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> CompiledStateGraph:
        """Compile legacy configuration into a native Deep Agent graph."""
        raw_config = config.get_all() if hasattr(config, "get_all") else dict(config)
        config_manager = config if isinstance(config, ConfigManager) else None
        agent_config = raw_config.get("AGENT_CONFIG", {})
        configured_type = agent_config.get("type", "react") if isinstance(agent_config, Mapping) else "react"
        if str(configured_type or "react").strip().lower() == "nl2sql":
            model_compiler = ModelConfigCompiler(raw_config)
            models = model_compiler.compile()
            requested_primary = agent_config.get("primary_model") if isinstance(agent_config, Mapping) else None
            primary_name = model_compiler.resolve_primary_model_name(
                models,
                str(requested_primary).strip() if requested_primary else None,
            )
            primary_model = models.get(primary_name)
            if primary_model is None:
                raise ValueError(f"Primary model '{primary_name}' is not available.")
            workspace = WorkspaceConfigCompiler(
                raw_config,
                user_id=user_id,
                session_id=session_id,
            ).compile()
            nl2sql_checkpointer = self._checkpointer
            if nl2sql_checkpointer is None:
                nl2sql_checkpointer = InMemorySaver()
            elif nl2sql_checkpointer is False:
                nl2sql_checkpointer = None
            return create_nl2sql_agent(
                raw_config,
                primary_model,
                workspace.backend,
                name=str(agent_config.get("id") or agent_config.get("name") or "nl2sql"),
                checkpointer=nl2sql_checkpointer,
                store=self._store,
                debug=self.is_debug_enabled(),
            )
        compiled_config = await DeepAgentConfigCompiler(
            raw_config,
            config_manager=config_manager,
            checkpointer=self._checkpointer,
            store=self._store,
            user_id=user_id,
            session_id=session_id,
        ).compile()
        return await create_data_agent(compiled_config)

    async def chat(
        self,
        user_query: str,
        session_id: str | None = None,
        initial_state: Mapping[str, Any] | None = None,
        checkpoint_id: str | None = None,
        config: RunnableConfig | None = None,
    ) -> dict[str, Any]:
        """Invoke the native Deep Agent and return its LangGraph state."""
        resolved_session_id = self._resolve_session_id(session_id, initial_state)
        resolved_user_id = self._resolve_user_id(initial_state)
        graph_input = self._prepare_message_state(user_query, initial_state)
        run_config = self._build_run_config(resolved_user_id, resolved_session_id, checkpoint_id, config)
        try:
            chat_agent = await self._get_chat_agent(resolved_user_id, resolved_session_id)
            return await chat_agent.ainvoke(graph_input, config=run_config)
        except DataAgentError:
            raise
        except Exception as exc:
            raise DataAgentError.from_exception(exc, component="sdk") from exc

    async def build_agent_graph(
        self,
        mode: str = "chat",
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> CompiledStateGraph:
        """Build and return the native Deep Agent graph."""
        if mode != "chat":
            raise ValueError(f"Unsupported agent graph mode: {mode!r}; only 'chat' is supported")
        resolved_user_id = validate_user_id(user_id or self._configured_user_id())
        resolved_session_id = validate_session_id(session_id or "default_session")
        return await self._get_chat_agent(resolved_user_id, resolved_session_id)

    def get_agent_info(self) -> dict[str, Any]:
        """Return the configured agent metadata."""
        agent_config = self.config.get("AGENT_CONFIG", {})
        return {
            "name": agent_config.get("name", "DataAgent"),
            "version": agent_config.get("version", "1.0"),
            "description": agent_config.get("description", "DataAgent"),
            "backend": self.backend,
            "has_config": bool(self.config),
        }

    def is_debug_enabled(self) -> bool:
        """Return whether the configured native agent should emit debug events."""
        value = self.config.get("AGENT_CONFIG.debug", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def name(self) -> str:
        """Return the configured agent name."""
        return str(self.get_agent_info().get("name", ""))

    def description(self) -> str:
        """Return the configured agent description."""
        return str(self.get_agent_info().get("description", "DataAgent"))

    def version(self) -> str:
        """Return the configured agent version."""
        return str(self.get_agent_info().get("version", "1.0"))

    def update_config(self, new_config: dict[str, Any]) -> None:
        """Update configuration and rebuild the graph on its next use."""
        self.config.update(new_config)
        self._chat_agent_instances.clear()

    async def get_node(self, node_name: str) -> Any:
        """Return a node from the compiled graph description."""
        chat_agent = await self._get_chat_agent(
            validate_user_id(self._configured_user_id()),
            validate_session_id("default_session"),
        )
        node = chat_agent.get_graph().nodes.get(node_name)
        if node is None:
            raise ValueError(f"Node '{node_name}' not found")
        return node

    @staticmethod
    def _build_run_config(
        user_id: str,
        session_id: str,
        checkpoint_id: str | None,
        base_config: RunnableConfig | None = None,
    ) -> RunnableConfig:
        run_config: dict[str, Any] = dict(base_config or {})
        configured = run_config.get("configurable", {})
        configurable = dict(configured) if isinstance(configured, Mapping) else {}
        configurable["thread_id"] = f"{user_id}:{session_id}"
        if checkpoint_id:
            configurable["checkpoint_id"] = checkpoint_id
        run_config["configurable"] = configurable
        return cast(RunnableConfig, run_config)

    @staticmethod
    def _new_session_id() -> str:
        prefix = datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S_")
        return prefix + str(uuid.uuid4())

    @staticmethod
    def _prepare_message_state(
        user_query: str,
        initial_state: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        messages = list((initial_state or {}).get("messages", []))
        messages.append(HumanMessage(content=user_query))
        state = dict(initial_state or {})
        state.update({"messages": messages})
        state.pop("session_id", None)
        state.pop("user_id", None)
        return state

    @staticmethod
    def _prepare_stream_input(input: Any, initial_state: Mapping[str, Any] | None) -> Any:
        if isinstance(input, str):
            return DataAgent._prepare_message_state(input, initial_state)
        if input is not None and not isinstance(input, Mapping):
            return input
        source = initial_state if initial_state is not None else input
        state = dict(source or {})
        user_query = str(state.get("user_query", "")).strip()
        messages = list(state.get("messages", []))
        if user_query:
            messages.append(HumanMessage(content=user_query))
        if not messages:
            raise ValueError("astream requires input messages or initial_state.user_query")
        state.update({"messages": messages})
        state.pop("session_id", None)
        state.pop("user_id", None)
        state.pop("user_query", None)
        return state

    @staticmethod
    def _resolve_session_id(session_id: str | None, initial_state: Mapping[str, Any] | None) -> str:
        if session_id and session_id.strip():
            return validate_session_id(session_id)
        initial_session_id = (initial_state or {}).get("session_id")
        if initial_session_id and str(initial_session_id).strip():
            return validate_session_id(str(initial_session_id))
        return validate_session_id(DataAgent._new_session_id())

    def _configured_user_id(self) -> str:
        configured = self.config.get("USER_ID", "anonymous")
        return str(configured or "anonymous").strip() or "anonymous"

    def _resolve_user_id(self, initial_state: Mapping[str, Any] | None) -> str:
        initial_user_id = (initial_state or {}).get("user_id")
        return validate_user_id(str(initial_user_id or self._configured_user_id()))

    async def _get_chat_agent(self, user_id: str, session_id: str) -> CompiledStateGraph:
        cache_key = (user_id, session_id)
        cached = self._chat_agent_instances.get(cache_key)
        if cached is not None:
            return cached
        async with self._chat_agent_lock:
            cached = self._chat_agent_instances.get(cache_key)
            if cached is None:
                cached = await self.select_engine(self.config, user_id=user_id, session_id=session_id)
                self._chat_agent_instances[cache_key] = cached
            return cached
