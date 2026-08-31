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
"""BaseHook Protocol for galatea-style hook chains.

A hook is a ``(state[, runtime]) -> state`` callable that can be attached to
nodes or agents as pre/post-processing steps.  Multiple hooks are chainable:
each receives the output of the previous one.

The framework inspects each hook's signature to decide whether to pass
``runtime``:

- If the hook declares ``runtime`` as its 2nd positional parameter
  (e.g. ``def hook(state, runtime)`` or ``def hook(state, runtime, *, config=None)``),
  the framework calls ``hook(state, runtime)``.
- If the hook does NOT declare ``runtime`` (e.g. ``def hook(state, *, config=None)``),
  the framework calls ``hook(state)`` only — config params are already bound
  via ``functools.partial`` from HOOKS YAML fields.
- If the hook declares ``**kwargs``, the framework calls
  ``hook(state, runtime=runtime)`` so that ``runtime`` is available
  inside ``kwargs`` if needed.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from dataagent.core.cbb.base_state import BaseState
    from dataagent.core.cbb.runtime import Runtime


class BaseHook(Protocol):
    def __call__(
        self,
        state: BaseState,
        runtime: Runtime | None = None,
    ) -> BaseState:
        """Call the hook."""
        pass


def invoke_hook(
    hook: Any,
    state: Any,
    runtime: Any = None,
    *,
    extra_kwargs: Mapping[str, Any] | None = None,
) -> Any:
    """Call a hook with the appropriate arguments based on its signature.

    Inspects the hook's parameters to decide whether ``runtime`` should be
    passed:

    - 2nd positional param (any name) → ``hook(state, runtime)``
    - ``**kwargs`` as 2nd param → ``hook(state, runtime=runtime)``
    - otherwise (e.g. ``(state, *, config=…)``) → ``hook(state)``
    - optional ``extra_kwargs`` → only forwarded when the hook explicitly accepts
      the keyword or declares ``**kwargs``.

    Config parameters bound via ``functools.partial`` are always forwarded
    automatically by Python's partial mechanism.

    Args:
        hook: Hook callable (possibly ``functools.partial``-wrapped).
        state: Workflow state / result.
        runtime: Per-invocation Runtime, or ``None``.
        extra_kwargs: Additional framework context for hooks that explicitly
            declare matching keyword-only parameters.

    Returns:
        The hook's return value (typically the updated state).
    """
    params = list(inspect.signature(hook).parameters.values())
    accepted_kwargs = _accepted_extra_kwargs(params, extra_kwargs)

    if len(params) >= 2:
        second = params[1]
        if second.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
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

    accepted_names = set()
    for param in params:
        is_keyword_only = param.kind == inspect.Parameter.KEYWORD_ONLY
        if is_keyword_only and param.name in extra_kwargs:
            accepted_names.add(param.name)
    return {name: extra_kwargs[name] for name in accepted_names}
