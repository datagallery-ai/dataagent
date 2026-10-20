"""Source-bound transformations around compiled workflow execution."""

import asyncio
import inspect
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from langgraph.runtime import Runtime

from dataagent.extensions.hooks import _validate_handler


@dataclass(frozen=True)
class RunHook:
    """A validated handler with isolated parameters and the owning Agent context."""

    event: str
    handler: Callable
    params: dict
    context: Any

    async def ainvoke(self, value: dict) -> dict:
        """Apply a state update before execution or replace the public result afterwards."""
        runtime = Runtime(context=self.context)
        original = deepcopy(value)
        arguments = (deepcopy(value), runtime)
        params = deepcopy(self.params)
        if inspect.iscoroutinefunction(self.handler):
            result = await self.handler(*arguments, params=params)
        else:
            result = await asyncio.to_thread(self.handler, *arguments, params=params)
        if result is None:
            return original
        if not isinstance(result, Mapping):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError(f"{self.event} must return a mapping or None")
        if self.event == "before_run":
            return {**original, **result}
        if original.get("error") and not result.get("error"):
            raise ValueError("after_run cannot replace a failed workflow result with success")
        return dict(result)


def compile_run_hooks(entries, loader, context) -> tuple[RunHook, ...]:
    """Bind handlers relative to their configuration source, never the consuming plugin."""
    hooks = []
    for root, spec in entries:
        handler = loader.load(root, spec.entrypoint)
        _validate_handler(spec.event, handler)
        hooks.append(RunHook(spec.event, handler, deepcopy(spec.params), context))
    return tuple(hooks)
