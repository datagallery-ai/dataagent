"""Bind the repository DataAgent runtime to the AG-UI transport."""

from __future__ import annotations

import hashlib
import json
from asyncio import Lock
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ag_ui.core.events import CustomEvent
from ag_ui.core.types import SystemMessage
from ag_ui_langgraph import LangGraphAgent
from dataagent import DataAgent
from dataagent.core.deepagents import DeepAgentConfigCompiler, create_data_agent
from dataagent.utils.runtime_paths import validate_session_id, validate_user_id
from langgraph.store.base import BaseStore
from langgraph.types import Checkpointer

from datafoundry_api.auth import Identity
from datafoundry_api.file_assets import FileAssetService
from datafoundry_api.mcp_servers import McpServerService, McpToolSelection
from datafoundry_api.model_profiles import RuntimeModelSelection
from datafoundry_api.skills import RuntimeSkillSelection, SkillService

_MAX_GRAPH_CACHE_SIZE = 128


class AgentScopeError(ValueError):
    """Raised when a client-provided user or thread identifier is unsafe."""


@dataclass(frozen=True)
class RunResourceConfig:
    """Frontend resource selections forwarded with one Agent run."""

    enabled_skill_ids: tuple[str, ...] | None = None
    active_skill_id: str | None = None
    enabled_mcp_server_ids: tuple[str, ...] | None = None


class ScopedLangGraphAgent(LangGraphAgent):
    """Keep the public AG-UI thread id while scoping checkpoint ids by user."""

    def __init__(
        self,
        *,
        user_id: str,
        unavailable_resources: tuple[dict[str, str], ...] = (),
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._user_id = user_id
        self._unavailable_resources = unavailable_resources

    async def run(self, input: Any) -> AsyncIterator[Any]:
        """Run with a user-scoped checkpoint thread and restore public event ids."""
        if self._unavailable_resources:
            yield CustomEvent(
                name="unavailable_resources",
                value={"resources": list(self._unavailable_resources)},
            )
            input = self._with_unavailable_resource_message(input)
        client_thread_id = input.thread_id
        scoped_thread_id = f"datafoundry:user:{self._user_id}:thread:{client_thread_id}"
        scoped_input = input.model_copy(update={"thread_id": scoped_thread_id})
        async for event in super().run(scoped_input):
            if getattr(event, "thread_id", None) == scoped_thread_id and hasattr(event, "model_copy"):
                event = event.model_copy(update={"thread_id": client_thread_id})
            yield event

    def _with_unavailable_resource_message(self, input: Any) -> Any:
        resources = json.dumps(self._unavailable_resources, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(resources.encode("utf-8")).hexdigest()[:16]
        details = "; ".join(
            f"{item.get('name', item.get('id', 'MCP'))}: {item.get('reason', 'unavailable')}"
            for item in self._unavailable_resources
        )
        notice = SystemMessage(
            id=f"unavailable-resources-{digest}",
            content=(
                "Some explicitly enabled MCP resources are unavailable for this run. "
                f"Continue with the remaining tools and disclose the limitation when relevant. Details: {details}"
            ),
        )
        return input.model_copy(update={"messages": [notice, *(input.messages or [])]})


class DataAgentRuntime:
    """Create isolated DataAgent graphs for authenticated users and threads."""

    def __init__(
        self,
        config_path: Path,
        *,
        checkpointer: Checkpointer,
        store: BaseStore,
        files: FileAssetService | None = None,
        skills: SkillService | None = None,
        mcp_servers: McpServerService | None = None,
        graph_cache_size: int = _MAX_GRAPH_CACHE_SIZE,
    ) -> None:
        self._config_path = config_path.expanduser().resolve()
        self._checkpointer = checkpointer
        self._store = store
        self._files = files
        self._skills = skills
        self._mcp_servers = mcp_servers
        self._data_agent: DataAgent | None = None
        self._graphs: OrderedDict[str, Any] = OrderedDict()
        self._graph_cache_size = max(1, graph_cache_size)
        self._lock = Lock()

    async def agent_for(
        self,
        user_id: str,
        thread_id: str,
        model_selection: RuntimeModelSelection | None = None,
        *,
        identity: Identity | None = None,
        resources: RunResourceConfig | None = None,
    ) -> LangGraphAgent:
        """Return a fresh AG-UI adapter backed by the user's cached thread graph."""
        try:
            resolved_user_id = validate_user_id(user_id)
            resolved_thread_id = validate_session_id(thread_id)
        except ValueError as exc:
            raise AgentScopeError(str(exc)) from exc
        if identity is not None and identity.user_id != resolved_user_id:
            raise AgentScopeError("Authenticated user does not match the requested Agent scope.")
        scoped_identity = identity or _synthetic_identity(resolved_user_id)
        resource_config = resources or RunResourceConfig()
        skill_selection = self._resolve_skills(scoped_identity, resource_config)
        workspace_tools = (
            self._files.workspace_tools(scoped_identity, resolved_thread_id) if self._files is not None else ()
        )
        mcp_selection = await self._resolve_mcp(scoped_identity, resource_config, workspace_tools)
        graph = await self._graph_for(
            resolved_user_id,
            resolved_thread_id,
            model_selection,
            skill_selection,
            mcp_selection,
            (*workspace_tools, *mcp_selection.tools),
        )
        data_agent = await self._load_data_agent()
        return ScopedLangGraphAgent(
            user_id=resolved_user_id,
            name="dataFoundry",
            description=data_agent.description(),
            graph=graph,
            unavailable_resources=mcp_selection.unavailable,
        )

    async def _graph_for(
        self,
        user_id: str,
        session_id: str,
        model_selection: RuntimeModelSelection | None,
        skill_selection: RuntimeSkillSelection,
        mcp_selection: McpToolSelection,
        tools: tuple[Any, ...],
    ) -> Any:
        base_agent = await self._load_data_agent()
        model_key = model_selection.cache_key if model_selection is not None else "server-default"
        cache_key = f"{user_id}:{session_id}:{model_key}:{skill_selection.cache_key}:{mcp_selection.cache_key}"
        async with self._lock:
            cached = self._graphs.get(cache_key)
            if cached is not None:
                self._graphs.move_to_end(cache_key)
                return cached
            config = base_agent.config.copy()
            config.set("USER_ID", user_id)
            if model_selection is not None and model_selection.model_slots is not None:
                model_slots = {
                    name: dict(slot) if isinstance(slot, Mapping) else slot
                    for name, slot in model_selection.model_slots.items()
                }
                config.set("MODEL", model_slots)
                config.set("AGENT_CONFIG.primary_model", model_selection.primary_model_name)
            if self._skills is not None:
                config.set("TOOLS.skills.user", list(skill_selection.names))
                if skill_selection.active_name:
                    _append_active_skill_prompt(config, skill_selection.active_name)
            raw_config = config.get_all()
            compiled = await DeepAgentConfigCompiler(
                raw_config,
                config_manager=config,
                tools=tools,
                checkpointer=self._checkpointer,
                store=self._store,
                user_id=user_id,
                session_id=session_id,
            ).compile()
            graph = await create_data_agent(compiled)
            self._graphs[cache_key] = graph
            while len(self._graphs) > self._graph_cache_size:
                self._graphs.popitem(last=False)
            return graph

    def _resolve_skills(self, identity: Identity, resources: RunResourceConfig) -> RuntimeSkillSelection:
        if self._skills is None:
            return RuntimeSkillSelection(names=(), active_name=None, cache_key="none")
        return self._skills.resolve_selection(identity, resources.enabled_skill_ids, resources.active_skill_id)

    async def _resolve_mcp(
        self,
        identity: Identity,
        resources: RunResourceConfig,
        workspace_tools: tuple[Any, ...],
    ) -> McpToolSelection:
        if self._mcp_servers is None:
            return McpToolSelection(tools=(), unavailable=(), cache_key="none")
        base_agent = await self._load_data_agent()
        reserved = _reserved_tool_names(base_agent.config.get_all())
        reserved.update(str(getattr(tool, "name", "")) for tool in workspace_tools)
        return await self._mcp_servers.resolve_tools(
            identity,
            resources.enabled_mcp_server_ids,
            reserved_names=reserved,
        )

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


def _synthetic_identity(user_id: str) -> Identity:
    return Identity(
        user_id=user_id,
        email=f"{user_id}@local.invalid",
        display_name=None,
        workspace_id=f"personal-{user_id}",
        workspace_name="Personal workspace",
        session_id="local",
        csrf_token_hash="",
        expires_at="",
    )


def _append_active_skill_prompt(config: Any, active_name: str) -> None:
    path = "SCENARIO.chat.prompt_appends.system"
    current = config.get(path, [])
    if isinstance(current, str):
        values = [current]
    elif isinstance(current, list):
        values = list(current)
    else:
        values = []
    values.append(
        f'The user selected the native Skill "{active_name}" as the priority Skill for this run. '
        "Prefer it when relevant, while keeping all other enabled Skills available."
    )
    config.set(path, values)


def _reserved_tool_names(config: Mapping[str, Any]) -> set[str]:
    names = {
        "edit_file",
        "glob",
        "grep",
        "list_workspace_files",
        "ls",
        "promote_workspace_file",
        "read_file",
        "read_workspace_file",
        "shell",
        "task",
        "write_file",
        "write_todos",
    }
    tools_config = config.get("TOOLS", {})
    if not isinstance(tools_config, Mapping):
        return names
    local_functions = tools_config.get("local_functions", [])
    if isinstance(local_functions, list):
        for entry in local_functions:
            if not isinstance(entry, Mapping):
                continue
            name = str(entry.get("name") or entry.get("function") or "").strip()
            if name:
                names.add(name)
    return names
