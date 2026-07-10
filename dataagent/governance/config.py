"""GOVERNANCE YAML validation and runtime registry."""

from __future__ import annotations

import importlib
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GovernanceRule:
    """One governance hook rule bound to one or more tools."""

    id: str
    applies_to: tuple[str, ...]
    address: str | Callable[..., Any]
    callable: Callable[..., Any]
    priority: int | None = None


class GovernanceConfig:
    """Resolved governance registry used by runtime governance adapters."""

    def __init__(
        self,
        *,
        invisible_tools: set[str] | None = None,
        policies_by_tool: Mapping[str, Sequence[GovernanceRule]] | None = None,
        injectors_by_tool: Mapping[str, Sequence[GovernanceRule]] | None = None,
    ) -> None:
        self._invisible_tools = set(invisible_tools or set())
        self._policies_by_tool = {name: list(rules) for name, rules in (policies_by_tool or {}).items()}
        self._injectors_by_tool = {name: list(rules) for name, rules in (injectors_by_tool or {}).items()}

    def is_tool_invisible(self, tool_name: str) -> bool:
        """Return whether ``tool_name`` should be hidden from LLM-visible tools."""
        return str(tool_name or "").strip() in self._invisible_tools

    def policies_for(self, tool_name: str) -> list[GovernanceRule]:
        """Return policy rules bound to ``tool_name`` in configured execution order."""
        return list(self._policies_by_tool.get(str(tool_name or "").strip(), []))

    def injectors_for(self, tool_name: str) -> list[GovernanceRule]:
        """Return argument injector rules bound to ``tool_name`` in configured execution order."""
        return list(self._injectors_by_tool.get(str(tool_name or "").strip(), []))


def validate_governance_config(raw: Any) -> None:
    """Validate top-level GOVERNANCE config shape without importing callables."""
    if raw is None:
        return
    if not isinstance(raw, Mapping):
        raise ValueError("GOVERNANCE must be a mapping")

    invisibility = raw.get("invisibility")
    if invisibility is not None:
        if not isinstance(invisibility, Sequence) or isinstance(invisibility, (str, bytes)):
            raise ValueError("GOVERNANCE.invisibility must be a list")
        for item in invisibility:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("GOVERNANCE.invisibility entries must be non-empty strings")

    _validate_rule_section(raw.get("policies"), section="policies")
    _validate_rule_section(raw.get("argument_injectors"), section="argument_injectors")


def build_governance_config(
    raw: Any,
    *,
    activated_suites: Sequence[Mapping[str, str]] | None = None,
) -> GovernanceConfig | None:
    """Build resolved governance registry from merged agent config."""
    if raw is None:
        return None
    validate_governance_config(raw)
    if not isinstance(raw, Mapping):
        return None

    invisible_tools = {str(item).strip() for item in raw.get("invisibility", []) or []}
    policies_by_tool = _build_rule_map(raw.get("policies"), section="policies", activated_suites=activated_suites)
    injectors_by_tool = _build_rule_map(
        raw.get("argument_injectors"),
        section="argument_injectors",
        activated_suites=activated_suites,
    )
    return GovernanceConfig(
        invisible_tools=invisible_tools,
        policies_by_tool=policies_by_tool,
        injectors_by_tool=injectors_by_tool,
    )


def _validate_rule_section(raw_rules: Any, *, section: str) -> None:
    if raw_rules is None:
        return
    label = f"GOVERNANCE.{section}"
    if not isinstance(raw_rules, Sequence) or isinstance(raw_rules, (str, bytes)):
        raise ValueError(f"{label} must be a list")

    seen_ids: set[str] = set()
    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, Mapping):
            raise ValueError(f"{label}[{index}] must be a mapping")
        rule_id = str(raw_rule.get("id") or "").strip()
        if not rule_id:
            raise ValueError(f"{label}[{index}] requires non-empty id")
        if rule_id in seen_ids:
            raise ValueError(f"Duplicate {label} id: {rule_id!r}")
        seen_ids.add(rule_id)

        applies_to = raw_rule.get("applies_to")
        if not isinstance(applies_to, Sequence) or isinstance(applies_to, (str, bytes)) or not applies_to:
            raise ValueError(f"{label}[{index}] requires non-empty applies_to list")
        for item in applies_to:
            tool_name = str(item or "").strip()
            if not tool_name:
                raise ValueError(f"{label}[{index}].applies_to entries must be non-empty strings")
            if tool_name == "*":
                raise ValueError(f"{label}[{index}].applies_to does not allow '*'")

        address = raw_rule.get("address")
        if not (callable(address) or isinstance(address, str) and address.strip()):
            raise ValueError(f"{label}[{index}] requires non-empty address")

        priority = raw_rule.get("priority")
        if priority is not None and not isinstance(priority, int):
            raise ValueError(f"{label}[{index}].priority must be an integer when provided")


def _build_rule_map(
    raw_rules: Any,
    *,
    section: str,
    activated_suites: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, list[GovernanceRule]]:
    rules_by_tool: dict[str, list[GovernanceRule]] = defaultdict(list)
    if raw_rules is None:
        return {}
    for raw_rule in raw_rules:
        assert isinstance(raw_rule, Mapping)
        address = raw_rule.get("address")
        resolved = address if callable(address) else _resolve_callable(str(address or "").strip(), activated_suites)
        applies_to = tuple(str(item).strip() for item in raw_rule.get("applies_to", []))
        rule = GovernanceRule(
            id=str(raw_rule.get("id") or "").strip(),
            applies_to=applies_to,
            address=address,
            callable=resolved,
            priority=raw_rule.get("priority"),
        )
        for tool_name in applies_to:
            rules_by_tool[tool_name].append(rule)
    return dict(rules_by_tool)


def _resolve_callable(
    address: str,
    activated_suites: Sequence[Mapping[str, str]] | None = None,
) -> Callable[..., Any]:
    if not address:
        raise ValueError("governance hook address cannot be empty")

    suite_callable = _try_resolve_suite_callable(address, activated_suites)
    if suite_callable is not None:
        return suite_callable

    if address.startswith("python:"):
        spec = address[len("python:") :]
        if ":" not in spec:
            raise ValueError(f"Invalid python governance hook address: {address!r}")
        module_name, attr_name = spec.rsplit(":", 1)
    else:
        if "." not in address:
            raise ValueError(f"Invalid governance hook address: {address!r}")
        module_name, attr_name = address.rsplit(".", 1)

    module = importlib.import_module(module_name)
    target = getattr(module, attr_name)
    if not callable(target):
        raise ValueError(f"Governance hook address is not callable: {address!r}")
    return target


def _try_resolve_suite_callable(
    address: str,
    activated_suites: Sequence[Mapping[str, str]] | None,
) -> Callable[..., Any] | None:
    """Resolve ``{suite_name}.relative.module.callable`` addresses from activated Suite roots."""
    ordered = sorted(
        (entry for entry in activated_suites or () if isinstance(entry, Mapping)),
        key=lambda item: len(str(item.get("name") or "")),
        reverse=True,
    )
    for entry in ordered:
        suite_name = str(entry.get("name") or "").strip()
        root_raw = str(entry.get("root") or "").strip()
        if not suite_name or not root_raw:
            continue
        prefix = f"{suite_name}."
        if not address.startswith(prefix):
            continue
        relative = address[len(prefix) :]
        if not relative:
            raise ValueError(f"Invalid Suite governance hook address: {address!r}")
        from dataagent.utils.import_utils import import_callable_from_suite_root

        return import_callable_from_suite_root(
            relative,
            root=Path(root_raw),
            suite_name=suite_name,
        )
    return None
