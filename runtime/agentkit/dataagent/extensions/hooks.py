"""Load, validate and adapt Python Hooks for Deep Agents' native middleware parameter."""

import asyncio
import inspect
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

from langchain.agents.middleware import (
    AgentMiddleware,
    after_agent,
    after_model,
    before_agent,
    before_model,
)
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from dataagent.declarations import HookSpec
from dataagent.extensions.loading import PythonLoader

_STATE_DECORATORS = {
    "before_agent": before_agent,
    "after_agent": after_agent,
    "before_model": before_model,
    "after_model": after_model,
}


def _validate_handler(event: str, handler: Callable) -> None:
    positional = 1 if event == "before_tool" else 2
    parameters = (
        list(inspect.signature(handler).parameters.values()) if inspect.isfunction(handler) else []
    )
    if (
        not inspect.isfunction(handler)
        or inspect.isgeneratorfunction(handler)
        or inspect.isasyncgenfunction(handler)
        or len(parameters) != positional + 1
        or any(
            parameter.kind
            not in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
            for parameter in parameters[:positional]
        )
        or parameters[-1].name != "params"
        or parameters[-1].kind != inspect.Parameter.KEYWORD_ONLY
    ):
        arguments = (
            "request"
            if event == "before_tool"
            else "request, result"
            if event == "after_tool"
            else "state, runtime"
        )
        raise TypeError(
            f"Hook {event} must be a plain def/async def with signature "
            f"handle({arguments}, *, params); migrate old handle(event, config) hooks"
        )


def _state_result(result):
    action = result.get("action") if isinstance(result, dict) else None
    if isinstance(action, str) and action in ("continue", "deny"):
        raise TypeError(
            "Hook action continue/deny was removed; return a native state update/Command, "
            "or raise an exception to reject execution"
        )
    if result is not None and not isinstance(result, (dict, Command)):
        if inspect.iscoroutine(result):
            result.close()
        raise TypeError("State Hook must return None, a native state-update dict, or Command")
    return result


def _tool_result(result, request, event):
    if result is None:
        return None
    expected_id = request.tool_call.get("id")
    if event == "before_tool" and isinstance(result, ToolCallRequest):
        if result.tool_call.get("id") != expected_id:
            raise ValueError("Tool Hook must preserve tool_call_id")
        return result
    if event in {"before_tool", "after_tool"}:
        if isinstance(result, ToolMessage):
            messages = [result]
        elif isinstance(result, Command) and isinstance(result.update, dict):
            messages = result.update.get("messages", [])
        else:
            messages = []
        if any(
            isinstance(message, ToolMessage) and message.tool_call_id == expected_id
            for message in messages
        ):
            return result
    if inspect.iscoroutine(result):
        result.close()
    raise TypeError(
        "Tool Hook must return None, a ToolCallRequest before execution, or a "
        "ToolMessage/Command with the matching tool_call_id (before execution short-circuits)"
    )


class _ToolHookAdapter(AgentMiddleware):
    """Expose before/after tool convenience points through the native tool wrapper."""

    def __init__(self, event: str, handler: Callable, params: dict, name: str, matcher: str | None):
        self.event, self.handler, self.params, self._name, self.matcher = (
            event,
            handler,
            params,
            name,
            matcher,
        )

    @property
    def name(self):
        return self._name

    def wrap_tool_call(self, request, handler):
        """Apply a synchronous Hook while retaining native request/result semantics."""
        if self.matcher is not None and request.tool_call.get("name") != self.matcher:
            return handler(request)
        if inspect.iscoroutinefunction(self.handler):
            raise TypeError("Async Hook requires ainvoke/astream, not a synchronous invocation")
        if self.event == "before_tool":
            updated = _tool_result(
                self.handler(request, params=deepcopy(self.params)), request, self.event
            )
            if isinstance(updated, (ToolMessage, Command)):
                return updated
            request = request if updated is None else updated
        result = handler(request)
        if self.event == "after_tool":
            updated = _tool_result(
                self.handler(request, result, params=deepcopy(self.params)), request, self.event
            )
            result = result if updated is None else updated
        return result

    async def awrap_tool_call(self, request, handler):
        """Apply an asynchronous Hook or run a synchronous handler off the event loop."""
        if self.matcher is not None and request.tool_call.get("name") != self.matcher:
            return await handler(request)

        async def call(*args):
            params = deepcopy(self.params)
            if inspect.iscoroutinefunction(self.handler):
                result = await self.handler(*args, params=params)
            else:
                result = await asyncio.to_thread(self.handler, *args, params=params)
            return _tool_result(result, request, self.event)

        if self.event == "before_tool":
            updated = await call(request)
            if isinstance(updated, (ToolMessage, Command)):
                return updated
            request = request if updated is None else updated
        result = await handler(request)
        if self.event == "after_tool":
            updated = await call(request, result)
            result = result if updated is None else updated
        return result


def compile_hook(spec: HookSpec, handler: Callable, *, name: str) -> AgentMiddleware:
    """Compile one declaration; native state/request/result objects remain untouched."""
    _validate_handler(spec.event, handler)
    params = deepcopy(spec.params)
    if spec.event in {"before_tool", "after_tool"}:
        return _ToolHookAdapter(spec.event, handler, params, name, spec.matcher)

    if inspect.iscoroutinefunction(handler):

        async def invoke(state, runtime):
            return _state_result(await handler(state, runtime, params=deepcopy(params)))
    else:

        def invoke(state, runtime):
            return _state_result(handler(state, runtime, params=deepcopy(params)))

    return _STATE_DECORATORS[spec.event](invoke, name=name)


def compile_hooks(
    hook_entries: list[tuple[Path, HookSpec]],
    loader: PythonLoader,
    *,
    agent_name: str,
) -> list[AgentMiddleware]:
    """Load and validate ordered Hooks relative to their declaration roots."""
    compiled_hooks = []
    for index, (root, spec) in enumerate(hook_entries):
        hook_handler = loader.load(root, spec.entrypoint)
        name = f"ConfiguredHook__{agent_name}__{spec.event}__{index}"
        try:
            compiled_hooks.append(compile_hook(spec, hook_handler, name=name))
        except TypeError as error:
            raise TypeError(f"Hook {spec.entrypoint!r} in {root}: {error}") from error
    return compiled_hooks
