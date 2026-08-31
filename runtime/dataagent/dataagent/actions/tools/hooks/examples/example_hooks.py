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
"""Reference implementations for per-tool-call pre/post hooks.

Wire in YAML under ``TOOLS.local_functions[].hooks``, ``TOOLS.mcp_servers[].hooks``,
or ``TOOLS.A2A[].<agent_id>.hooks`` using dotted ``module.path.callable`` specs, for example::

    hooks:
      pre:
        - dataagent.actions.tools.hooks.examples.example_hooks.audit_pre
      post:
        - dataagent.actions.tools.hooks.examples.example_hooks.audit_post

``inv.tool_args`` is a shallow read-only mapping; use ``inv.hook_context`` for shared state.
"""

from __future__ import annotations

from loguru import logger

from dataagent.actions.tools.hooks.base import (
    ToolHookInvocation,
    ToolPostHookOutcome,
    ToolPreHookOutcome,
)


async def audit_pre(inv: ToolHookInvocation) -> ToolPreHookOutcome:
    """Example pre-hook: log tool name and stash audit metadata in ``hook_context``.

    Args:
        inv: Per-call hook context (``tool_args`` is read-only).

    Returns:
        Empty outcome; failures should ``raise`` (e.g. ``ValueError``).

    Raises:
        ValueError: Example guard when ``require_non_empty_args`` is set in hook_context
            by a prior hook (not used in the stock example).
    """
    inv.hook_context.setdefault("audit", []).append(
        {"phase": "pre", "tool": inv.tool_name, "call_id": inv.tool_call_id}
    )
    logger.debug(
        "[example_hooks] pre tool={} call_id={} arg_keys={}",
        inv.tool_name,
        inv.tool_call_id,
        list(inv.tool_args.keys()),
    )
    return ToolPreHookOutcome()


async def audit_post(inv: ToolHookInvocation) -> ToolPostHookOutcome:
    """Example post-hook: log normalized execution outcome (strategy A: runs after tool success or failure).

    Args:
        inv: Per-call context with ``execution`` set; ``tool_result`` may be ``None`` on tool failure.

    Returns:
        Empty outcome.
    """
    success = inv.execution.success if inv.execution else None
    inv.hook_context.setdefault("audit", []).append(
        {"phase": "post", "tool": inv.tool_name, "success": success}
    )
    logger.debug(
        "[example_hooks] post tool={} call_id={} success={}",
        inv.tool_name,
        inv.tool_call_id,
        success,
    )
    return ToolPostHookOutcome()


def noop_probe_tool(label: str = "") -> dict[str, str]:
    """Minimal local tool for YAML hook wiring smoke tests (not for production agents).

    Args:
        label: Optional string echoed in ``original_msg``.

    Returns:
        Structured tool payload compatible with Executor normalization.
    """
    text = label or "ok"
    return {"original_msg": text, "frontend_msg": text}
