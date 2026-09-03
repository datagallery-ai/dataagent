"""Compile compatible tool hooks into native LangChain middleware."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeAlias

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command

from dataagent.core.deepagents.state import DataAgentState
from dataagent.utils.import_utils import import_callable
from dataagent.utils.log import logger

_TOOL_SOURCE_KEY = "dataagent_tool_source"
_TOOL_SOURCE_ID_KEY = "dataagent_tool_source_id"

ToolHookResult: TypeAlias = ToolMessage | Command[Any]
ToolPreHook: TypeAlias = Callable[[ToolCallRequest], ToolCallRequest | None | Awaitable[ToolCallRequest | None]]
ToolPostHook: TypeAlias = Callable[
    [ToolCallRequest, ToolHookResult], ToolHookResult | None | Awaitable[ToolHookResult | None]
]
_ToolHookKey: TypeAlias = tuple[str, str]


@dataclass(frozen=True)
class _ResolvedToolHook:
    """One imported hook and its YAML location."""

    callable: Callable[..., Any]
    location: str


@dataclass(frozen=True)
class _ToolHookChain:
    """Ordered pre- and post-hook chains for one tagged tool source."""

    pre: tuple[_ResolvedToolHook, ...]
    post: tuple[_ResolvedToolHook, ...]


def tag_tool(tool: BaseTool, source: str, source_id: str) -> BaseTool:
    """Return a copy of a tool tagged with the YAML source that created it."""
    metadata = dict(tool.metadata or {})
    metadata.update({_TOOL_SOURCE_KEY: source, _TOOL_SOURCE_ID_KEY: source_id})
    return tool.model_copy(update={"metadata": metadata})


class ToolHookMiddleware(AgentMiddleware[DataAgentState, None, Any]):
    """Run native asynchronous tool hooks selected by tagged tool source."""

    state_schema = DataAgentState

    def __init__(self, hooks: Mapping[_ToolHookKey, _ToolHookChain]) -> None:
        self._hooks = dict(hooks)
        self._local_hooks = {
            source_id: chain for (source, source_id), chain in self._hooks.items() if source == "local"
        }

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolHookResult]],
    ) -> ToolHookResult:
        """Run configured hooks around the native tool handler.

        Hook failures become an error ``ToolMessage``. Exceptions raised by the
        actual handler are intentionally left to LangChain's default tool error
        handling.
        """
        hooks = self._hooks_for(request)
        if hooks is None:
            return await handler(request)

        try:
            current_request = await self._run_pre_hooks(hooks.pre, request)
        except Exception as exc:
            return self._hook_failure(request, "pre", exc)

        result = await handler(current_request)
        try:
            return await self._run_post_hooks(hooks.post, current_request, result)
        except Exception as exc:
            return self._hook_failure(current_request, "post", exc)

    def _hooks_for(self, request: ToolCallRequest) -> _ToolHookChain | None:
        tool = request.tool
        metadata = tool.metadata if tool is not None and tool.metadata is not None else {}
        source = str(metadata.get(_TOOL_SOURCE_KEY, "")).strip()
        source_id = str(metadata.get(_TOOL_SOURCE_ID_KEY, "")).strip()
        if source or source_id:
            return self._hooks.get((source, source_id))
        if tool is None:
            return None
        return self._local_hooks.get(tool.name)

    @staticmethod
    async def _run_pre_hooks(
        hooks: Sequence[_ResolvedToolHook],
        request: ToolCallRequest,
    ) -> ToolCallRequest:
        current_request = request
        for hook in hooks:
            result = hook.callable(current_request)
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                continue
            if not isinstance(result, ToolCallRequest):
                raise TypeError(f"{hook.location} must return ToolCallRequest or None, got {type(result).__name__}.")
            current_request = result
        return current_request

    @staticmethod
    async def _run_post_hooks(
        hooks: Sequence[_ResolvedToolHook],
        request: ToolCallRequest,
        result: ToolHookResult,
    ) -> ToolHookResult:
        current_result = result
        for hook in hooks:
            updated_result = hook.callable(request, current_result)
            if inspect.isawaitable(updated_result):
                updated_result = await updated_result
            if updated_result is None:
                continue
            if not isinstance(updated_result, (ToolMessage, Command)):
                raise TypeError(
                    f"{hook.location} must return ToolMessage, Command, or None, got {type(updated_result).__name__}."
                )
            current_result = updated_result
        return current_result

    @staticmethod
    def _hook_failure(request: ToolCallRequest, phase: str, exc: Exception) -> ToolMessage:
        tool_call = request.tool_call
        tool_name = str(tool_call.get("name", "")).strip()
        tool_call_id = str(tool_call.get("id") or request.runtime.tool_call_id or "unknown")
        logger.exception("{} hook failed for tool '{}': {}", phase, tool_name, exc)
        return ToolMessage(
            content=f"Tool {phase}-hook failed for '{tool_name}': {exc}",
            name=tool_name or None,
            tool_call_id=tool_call_id,
            status="error",
        )


class ToolHookConfigCompiler:
    """Compile legacy ``TOOLS.*.hooks`` locations into native tool middleware."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._config = config

    def compile(self) -> ToolHookMiddleware | None:
        """Return native tool-hook middleware, or ``None`` when no hooks resolve."""
        tools_config = self._tools_config()
        hooks: dict[_ToolHookKey, _ToolHookChain] = {}
        self._compile_local_hooks(tools_config, hooks)
        self._compile_mcp_hooks(tools_config, hooks)
        self._compile_a2a_hooks(tools_config, hooks)
        return ToolHookMiddleware(hooks) if hooks else None

    def _compile_local_hooks(
        self,
        tools_config: Mapping[str, Any],
        hooks: dict[_ToolHookKey, _ToolHookChain],
    ) -> None:
        entries = self._as_list(tools_config.get("local_functions", []), "TOOLS.local_functions")
        for index, raw_entry in enumerate(entries):
            path = f"TOOLS.local_functions[{index}]"
            entry = self._as_mapping(raw_entry, path)
            tool_name = str(entry.get("name") or entry.get("function") or "").strip()
            if tool_name:
                self._add_chain(hooks, ("local", tool_name), entry.get("hooks"), f"{path}.hooks")

    def _compile_mcp_hooks(
        self,
        tools_config: Mapping[str, Any],
        hooks: dict[_ToolHookKey, _ToolHookChain],
    ) -> None:
        entries = self._as_list(tools_config.get("mcp_servers", []), "TOOLS.mcp_servers")
        for index, raw_entry in enumerate(entries):
            path = f"TOOLS.mcp_servers[{index}]"
            entry = self._as_mapping(raw_entry, path)
            server_id = str(entry.get("server_id") or entry.get("name") or "").strip()
            if server_id:
                self._add_chain(hooks, ("mcp", server_id), entry.get("hooks"), f"{path}.hooks")

    def _compile_a2a_hooks(
        self,
        tools_config: Mapping[str, Any],
        hooks: dict[_ToolHookKey, _ToolHookChain],
    ) -> None:
        entries = self._as_list(tools_config.get("A2A", []), "TOOLS.A2A")
        for index, raw_entry in enumerate(entries):
            path = f"TOOLS.A2A[{index}]"
            entry = self._as_mapping(raw_entry, path)
            agent_id, agent_config = self._a2a_entry(entry, path)
            if agent_id:
                self._add_chain(hooks, ("a2a", agent_id), agent_config.get("hooks"), f"{path}.hooks")

    def _add_chain(
        self,
        hooks: dict[_ToolHookKey, _ToolHookChain],
        key: _ToolHookKey,
        raw_hooks: Any,
        path: str,
    ) -> None:
        if raw_hooks is None:
            return
        if not isinstance(raw_hooks, Mapping):
            logger.warning("{} must be a mapping; skipping configured tool hooks.", path)
            return
        hook_config = raw_hooks
        pre = self._resolve_hooks(hook_config, "pre", f"{path}.pre", 1)
        post = self._resolve_hooks(hook_config, "post", f"{path}.post", 2)
        if pre or post:
            hooks[key] = _ToolHookChain(pre=pre, post=post)

    def _resolve_hooks(
        self,
        hook_config: Mapping[str, Any],
        phase: str,
        path: str,
        argument_count: int,
    ) -> tuple[_ResolvedToolHook, ...]:
        raw_hooks = hook_config.get(phase)
        if raw_hooks is None:
            return ()
        if not isinstance(raw_hooks, list):
            logger.warning("{} must be a list; skipping configured {} hooks.", path, phase)
            return ()

        resolved: list[_ResolvedToolHook] = []
        for index, raw_hook in enumerate(raw_hooks):
            location = f"{path}[{index}]"
            spec = str(raw_hook or "").strip()
            if not spec:
                continue
            try:
                hook = import_callable(spec)
                self._validate_signature(hook, argument_count)
            except Exception as exc:
                logger.warning("Skipping tool hook '{}' at {}: {}", spec, location, exc)
                continue
            resolved.append(_ResolvedToolHook(callable=hook, location=location))
        return tuple(resolved)

    @staticmethod
    def _validate_signature(hook: Callable[..., Any], argument_count: int) -> None:
        signature = inspect.signature(hook)
        signature.bind(*([object()] * argument_count))

    def _tools_config(self) -> Mapping[str, Any]:
        raw = self._config.get("TOOLS", {})
        if raw is None:
            return {}
        return self._as_mapping(raw, "TOOLS")

    @staticmethod
    def _as_mapping(value: Any, path: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{path} must be a mapping.")
        return value

    @staticmethod
    def _as_list(value: Any, path: str) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise ValueError(f"{path} must be a list.")
        return list(value)

    def _a2a_entry(self, entry: Mapping[str, Any], path: str) -> tuple[str, Mapping[str, Any]]:
        agent_id = str(entry.get("agent_id") or entry.get("name") or "").strip()
        if agent_id:
            return agent_id, entry
        if len(entry) != 1:
            return "", {}
        raw_agent_id, raw_agent_config = next(iter(entry.items()))
        agent_id = str(raw_agent_id).strip()
        if not agent_id:
            return "", {}
        return agent_id, self._as_mapping(raw_agent_config, f"{path}.{agent_id}")
