# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Build OpenJiuWen LocalFunction tools from normalized DataAgent YAML."""

from __future__ import annotations

import asyncio
import importlib
import inspect
from collections.abc import Callable, Iterable
from functools import wraps
from typing import TYPE_CHECKING, Any

from dataagent.core.deep_agent.spec import LocalToolSpec

if TYPE_CHECKING:
    from openjiuwen.core.foundation.tool import Tool


def build_local_tools(specs: Iterable[LocalToolSpec]) -> list[Tool]:
    """Import configured Python callables and adapt them to Jiuwen tools."""
    tools: list[Tool] = []
    for spec in specs:
        func = _load_callable(spec)
        _validate_callable(func, spec)
        adapted_func = func if inspect.iscoroutinefunction(func) else _wrap_sync_callable(func)

        from openjiuwen.core.foundation.tool import tool

        local_tool = tool(
            adapted_func,
            name=spec.name,
            description=spec.description,
        )
        local_tool.card.properties.update(
            {
                "dataagent.category": spec.category,
                "dataagent.module": spec.module,
                "dataagent.function": spec.function,
            }
        )
        tools.append(local_tool)
    return tools


def _load_callable(spec: LocalToolSpec) -> Callable[..., Any]:
    try:
        module = importlib.import_module(spec.module)
    except Exception as exc:
        raise ValueError(f"{spec.path}.module failed to import {spec.module!r}: {exc}") from exc

    try:
        func = getattr(module, spec.function)
    except AttributeError as exc:
        raise ValueError(
            f"{spec.path}.function {spec.function!r} was not found in module {spec.module!r}"
        ) from exc

    if not callable(func) or inspect.isclass(func):
        raise ValueError(f"{spec.path} target {spec.module}.{spec.function} must be a Python function")
    return func


def _validate_callable(func: Callable[..., Any], spec: LocalToolSpec) -> None:
    if inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(func):
        raise ValueError(
            f"{spec.path} target {spec.module}.{spec.function} is a generator; "
            "DeepAgent local tools must return a final value from invoke()"
        )

    signature = inspect.signature(func)
    if "_tool_context" in signature.parameters:
        raise ValueError(
            f"{spec.path} target {spec.module}.{spec.function} requires _tool_context, which is not supported by "
            "the OpenJiuWen local tool adapter. Migrate subagent behavior to the Subagent adapter."
        )


def _wrap_sync_callable(func: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(func)
    async def invoke_in_thread(*args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(func, *args, **kwargs)

    return invoke_in_thread
