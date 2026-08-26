"""Tool-hook adapters for GOVERNANCE policy and argument injector rules."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dataagent.governance.config import GovernanceConfig, GovernanceRule


@dataclass
class GovernanceInvocation:
    """Context passed to governance policy and argument injector hooks."""

    tool_name: str
    tool_call_id: str
    tool_args: dict[str, Any]
    runtime: Any
    metadata: dict[str, Any]
    config: dict[str, Any]


def attach_governance_hooks_to_tool(governance: GovernanceConfig, tool: Any, tool_name: str) -> None:
    """Attach governance rules for ``tool_name`` to a tool instance's pre-hooks."""
    name = str(tool_name or "").strip()
    if not name:
        return
    governance_hooks: list[Callable[..., Any]] = []
    governance_hooks.extend(_policy_hook(rule) for rule in governance.policies_for(name))
    governance_hooks.extend(_injector_hook(rule) for rule in governance.injectors_for(name))
    if not governance_hooks:
        return

    current = list(getattr(tool, "pre_hooks", None) or [])
    tool.pre_hooks = governance_hooks + current


def _policy_hook(rule: GovernanceRule) -> Callable[..., Any]:
    async def run_policy(inv: Any) -> None:
        governance_inv = _governance_invocation(inv)
        result = rule.callable(governance_inv)
        if inspect.isawaitable(result):
            await result

    return run_policy


def _injector_hook(rule: GovernanceRule) -> Callable[..., Any]:
    async def run_injector(inv: Any) -> None:
        before = dict(getattr(inv, "tool_args", {}) or {})
        governance_inv = _governance_invocation(inv)
        result = rule.callable(governance_inv)
        if inspect.isawaitable(result):
            result = await result
        if result is not None and not isinstance(result, dict):
            raise TypeError(
                f"governance argument injector {rule.id!r} must return dict or None, got {type(result).__name__}"
            )
        if isinstance(result, dict):
            governance_inv.tool_args.update(result)
        _validate_internal_arg_changes(before, governance_inv.tool_args)

    return run_injector


def _governance_invocation(inv: Any) -> GovernanceInvocation:
    runtime = getattr(inv, "runtime", None)
    config: dict[str, Any] = {}
    if runtime is not None and hasattr(runtime, "get_all_config"):
        config = runtime.get_all_config() or {}
    return GovernanceInvocation(
        tool_name=str(getattr(inv, "tool_name", "") or ""),
        tool_call_id=str(getattr(inv, "tool_call_id", "") or ""),
        tool_args=getattr(inv, "tool_args", {}),
        runtime=runtime,
        metadata=getattr(inv, "metadata", {}),
        config=config,
    )


def _validate_internal_arg_changes(before: dict[str, Any], after: dict[str, Any]) -> None:
    changed: set[str] = set()
    for key, value in after.items():
        if key not in before or before.get(key) != value:
            changed.add(str(key))
    for key in changed:
        if not key.startswith("_"):
            raise ValueError("governance argument injectors may only inject underscore-prefixed internal args")
