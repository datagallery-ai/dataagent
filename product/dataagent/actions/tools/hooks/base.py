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
"""Per-tool-call hook types and runner (Flex Executor)."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from dataagent.core.utils.performance import callable_perf_name, get_current_collector

if TYPE_CHECKING:
    from dataagent.core.cbb.runtime import Runtime
    from dataagent.core.flex.nodes.executor import NormalizedToolExecution
    from dataagent.core.managers.action_manager.base import ToolResult


@dataclass
class ToolHookInvocation:
    """Context passed to each pre/post hook for one tool call.

    ``hook_context`` is shared across all hooks in the same tool call (pre and post).
    ``tool_args`` is the mutable argument dict passed to tool invocation.
    """

    tool_name: str
    tool_call_id: str
    tool_args: dict[str, Any]
    runtime: Runtime
    metadata: dict[str, Any]
    hook_context: dict[str, Any] = field(default_factory=dict)
    phase: Literal["pre", "post"] = "pre"
    tool_result: ToolResult | None = None
    execution: NormalizedToolExecution | None = None


@dataclass
class ToolPreHookOutcome:
    """reserved return type; runner ignores the value. Failures use exceptions."""


@dataclass
class ToolPostHookOutcome:
    """reserved return type; runner ignores the value. Failures use exceptions."""


class ToolPreHook(Protocol):
    """Optional typing protocol for pre-hooks."""

    async def __call__(self, inv: ToolHookInvocation) -> ToolPreHookOutcome: ...


class ToolPostHook(Protocol):
    """Optional typing protocol for post-hooks."""

    async def __call__(self, inv: ToolHookInvocation) -> ToolPostHookOutcome: ...


class ToolHookRunner:
    """Execute ordered pre/post hook chains for a single tool call."""

    @staticmethod
    async def run_pre_hooks(hooks: list[Any], inv: ToolHookInvocation) -> None:
        """Run pre-hooks in order; hooks may mutate ``inv.tool_args`` and ``inv.hook_context``.

        Args:
            hooks: Callables ``(inv) -> ToolPreHookOutcome`` (sync or async).
            inv: Invocation context; ``phase`` is set to ``"pre"`` before each hook.

        Raises:
            Exception: Propagates the first hook failure to the Executor boundary.
        """
        inv.phase = "pre"
        for hook in hooks:
            await ToolHookRunner._invoke_hook(hook, inv)

    @staticmethod
    async def run_post_hooks(hooks: list[Any], inv: ToolHookInvocation) -> None:
        """Run post-hooks in order after tool execution has been normalized.

        Args:
            hooks: Callables ``(inv) -> ToolPostHookOutcome`` (sync or async).
            inv: Invocation with ``execution`` (and optional ``tool_result``) set.

        Raises:
            Exception: Propagates the first hook failure to the Executor boundary.
        """
        inv.phase = "post"
        for hook in hooks:
            await ToolHookRunner._invoke_hook(hook, inv)

    @staticmethod
    async def _invoke_hook(hook: Any, inv: ToolHookInvocation) -> None:
        """Invoke one hook and discard its return value."""
        collector = get_current_collector()
        with collector.measure(
            "hook",
            callable_perf_name(hook),
            hook_scope="tool",
            hook_phase=inv.phase,
        ):
            if inspect.iscoroutinefunction(hook):
                await hook(inv)
            else:
                result = hook(inv)
                if inspect.isawaitable(result):
                    await result
