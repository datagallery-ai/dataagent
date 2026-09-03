"""Compatibility invocation helpers for native agent hooks."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any


def invoke_hook(
    hook: Any,
    state: Any,
    runtime: Any = None,
    *,
    extra_kwargs: Mapping[str, Any] | None = None,
) -> Any:
    """Invoke a hook with only the runtime and framework arguments it accepts."""
    params = list(inspect.signature(hook).parameters.values())
    accepted_kwargs = _accepted_extra_kwargs(params, extra_kwargs)

    if len(params) >= 2:
        second = params[1]
        if second.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            return hook(state, runtime, **accepted_kwargs)
        if second.kind == inspect.Parameter.VAR_KEYWORD:
            return hook(state, runtime=runtime, **accepted_kwargs)

    return hook(state, **accepted_kwargs)


def _accepted_extra_kwargs(
    params: list[inspect.Parameter],
    extra_kwargs: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not extra_kwargs:
        return {}
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params):
        return dict(extra_kwargs)

    accepted_names = {
        param.name for param in params if param.kind == inspect.Parameter.KEYWORD_ONLY and param.name in extra_kwargs
    }
    return {name: extra_kwargs.get(name) for name in accepted_names}
