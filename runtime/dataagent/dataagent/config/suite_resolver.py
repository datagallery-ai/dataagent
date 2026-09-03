"""Resolve Suite bundles into ordinary native DataAgent configuration layers."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from dataagent.core.suite.activation import activate_suites, order_suites_for_merge
from dataagent.core.suite.discovery import discover_suite_index
from dataagent.core.suite.types import SuiteRecord
from dataagent.utils.import_utils import import_callable_from_suite_root, register_callable_spec

_PROMPT_SUFFIXES = frozenset({".jinja", ".jinja2", ".j2", ".md", ".txt"})
_UNSUPPORTED_SUITE_PATHS = (
    ("resources/resources.yaml", "resource definitions"),
    ("scenarios", "legacy scenario definitions"),
    ("node_configs.yaml", "legacy node configuration"),
)
_RETIRED_SUITE_LOCAL_TOOLS = frozenset(
    {
        "advance_data_analysis_workflow",
        "bash",
        "cancel_job",
        "cancel_subagent",
        "collect_job",
        "collect_subagent",
        "control_data_analysis_workflow",
        "inspect_data_analysis_workflow",
        "inspect_workspace",
        "list_resources",
        "poll_job",
        "poll_subagent",
        "search_workspaces",
        "start_data_analysis_workflow",
        "sub_agent_tool",
        "submit_resource_job",
        "submit_subagent",
    }
)


@dataclass(frozen=True)
class ResolvedSuiteConfig:
    """Native configuration layers and metadata produced from activated Suites."""

    layers: tuple[dict[str, Any], ...]
    activated_suites: tuple[dict[str, str], ...]


class SuiteConfigResolver:
    """Expand Suite directories before the native agent configuration is compiled."""

    def resolve(self, suite_config: Mapping[str, Any] | None) -> ResolvedSuiteConfig:
        """Resolve ``SUITE.include`` into ordered ordinary configuration layers."""
        index = discover_suite_index()
        activated = activate_suites(suite_config=suite_config, index=index)
        ordered = order_suites_for_merge(activated)

        layers: list[dict[str, Any]] = []
        activated_suites: list[dict[str, str]] = []
        for suite in ordered:
            layer = self._build_layer(suite)
            if layer:
                layers.append(layer)
            activated_suites.append({"name": suite.name, "root": str(suite.root)})
        return ResolvedSuiteConfig(layers=tuple(layers), activated_suites=tuple(activated_suites))

    def _build_layer(self, suite: SuiteRecord) -> dict[str, Any]:
        """Parse one Suite bundle into generic DataAgent configuration sections."""
        layer = self._load_mapping(suite.root / "models.yaml")
        tools = self._load_tools(suite)
        if tools:
            self._merge_mapping(layer, {"TOOLS": tools})

        hooks = self._load_hooks(suite)
        if hooks:
            self._merge_mapping(layer, {"HOOKS": hooks})

        governance = self._load_governance(suite)
        if governance:
            self._merge_mapping(layer, {"GOVERNANCE": governance})
            logger.warning(
                "Suite '{}' contributes GOVERNANCE, which is retained in effective config but is not active yet "
                "in the native runtime.",
                suite.name,
            )

        skills = self._load_skills(suite.root)
        if skills:
            self._merge_mapping(layer, {"TOOLS": {"skills": skills}})

        prompts = self._load_prompts(suite.root)
        if prompts:
            self._merge_mapping(layer, {"SCENARIO": {"chat": {"prompt_appends": prompts}}})

        subagents = self._load_subagents(suite.root)
        if subagents:
            self._merge_mapping(layer, {"SUBAGENTS": subagents})

        self._warn_ignored_legacy_content(suite)
        return layer

    @staticmethod
    def _load_mapping(path: Path) -> dict[str, Any]:
        """Load one optional YAML mapping, returning an empty mapping when absent or non-mapping."""
        if not path.is_file():
            return {}
        with path.open(encoding="utf-8") as handle:
            document = yaml.safe_load(handle) or {}
        return dict(document) if isinstance(document, Mapping) else {}

    def _load_tools(self, suite: SuiteRecord) -> dict[str, Any]:
        """Load ordinary tool declarations and compatible replacement metadata."""
        document = self._load_mapping(suite.root / "tools" / "tools.yaml")
        tools = document.get("TOOLS")
        result = dict(tools) if isinstance(tools, Mapping) else {}
        self._remove_retired_local_tools(result, suite.name)
        replaces = suite.meta.get("replaces")
        if isinstance(replaces, Sequence) and not isinstance(replaces, (str, bytes)):
            result.update({"_replaces": [str(item).strip() for item in replaces if str(item).strip()]})
            logger.warning(
                "Suite '{}' declares tool replacements; TOOLS._replaces is retained in effective config, "
                "but native Deep Agents replacement behavior is not active yet.",
                suite.name,
            )
        return result

    @staticmethod
    def _remove_retired_local_tools(tools: dict[str, Any], suite_name: str) -> None:
        """Drop retired job, subagent, and legacy shell tools from one Suite layer."""
        entries = tools.get("local_functions")
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            return

        retained: list[Any] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                retained.append(entry)
                continue
            function_name = str(entry.get("function") or entry.get("name") or "").strip()
            tool_name = str(entry.get("name") or function_name).strip()
            if function_name not in _RETIRED_SUITE_LOCAL_TOOLS and tool_name not in _RETIRED_SUITE_LOCAL_TOOLS:
                retained.append(entry)
                continue
            logger.warning("Suite '{}' retired local tool '{}' was ignored.", suite_name, tool_name or function_name)
        tools.update({"local_functions": retained})

    def _load_hooks(self, suite: SuiteRecord) -> dict[str, Any]:
        """Load Suite hooks as generic registered callable specifications."""
        document = self._load_mapping(suite.root / "hooks" / "hooks.yaml")
        hooks = document.get("HOOKS")
        if not isinstance(hooks, Mapping):
            return {}
        resolved = self._resolve_hook_node(hooks, suite, location="HOOKS")
        return dict(resolved) if isinstance(resolved, Mapping) else {}

    def _resolve_hook_node(self, node: Any, suite: SuiteRecord, *, location: str) -> Any:
        """Resolve Suite-local hook entries without exposing Suite paths to the compiler."""
        if isinstance(node, Mapping):
            return {
                str(key): self._resolve_hook_node(value, suite, location=f"{location}.{key}")
                for key, value in node.items()
            }
        if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            resolved = [
                self._resolve_hook_item(item, suite, location=f"{location}[{index}]") for index, item in enumerate(node)
            ]
            return [item for item in resolved if item is not None]
        return node

    def _resolve_hook_item(self, item: Any, suite: SuiteRecord, *, location: str) -> Any:
        """Resolve one Suite hook item while retaining its normal HOOKS YAML shape."""
        if isinstance(item, str):
            return self._resolve_callable_spec(item, suite, location=location)
        if not isinstance(item, Mapping):
            return item

        result = dict(item)
        name = str(result.get("name") or "").strip()
        if not name:
            return result
        resolved = self._resolve_callable_spec(name, suite, location=f"{location}.name")
        if resolved is None:
            return None
        result.update({"name": resolved})
        return result

    def _load_governance(self, suite: SuiteRecord) -> dict[str, Any]:
        """Load governance declarations as ordinary effective-config values."""
        document = self._load_mapping(suite.root / "governance" / "governance.yaml")
        governance = document.get("GOVERNANCE")
        if not isinstance(governance, Mapping):
            return {}

        result = dict(governance)
        for section in ("policies", "argument_injectors"):
            entries = result.get(section)
            if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
                continue
            resolved_entries: list[Any] = []
            for index, entry in enumerate(entries):
                if not isinstance(entry, Mapping):
                    resolved_entries.append(entry)
                    continue
                resolved_entry = dict(entry)
                address = str(resolved_entry.get("address") or "").strip()
                if address:
                    resolved = self._resolve_callable_spec(
                        address,
                        suite,
                        location=f"GOVERNANCE.{section}[{index}].address",
                    )
                    if resolved is None:
                        continue
                    resolved_entry.update({"address": resolved})
                resolved_entries.append(resolved_entry)
            result.update({section: resolved_entries})
        return result

    @staticmethod
    def _load_skills(root: Path) -> dict[str, list[str]]:
        """Expose a Suite skill directory through normal ``TOOLS.skills`` configuration."""
        skill_dir = root / "skill"
        if not skill_dir.is_dir():
            return {}
        return {"custom_dirs": [str(skill_dir.resolve())]}

    @staticmethod
    def _load_prompts(root: Path) -> dict[str, list[str]]:
        """Read Suite prompt files into generic static prompt append lists."""
        prompts_dir = root / "prompts"
        if not prompts_dir.is_dir():
            return {}

        result: dict[str, list[str]] = {}
        for prompt_type in ("system", "user"):
            directory = prompts_dir / prompt_type
            if not directory.is_dir():
                continue
            contents = [
                path.read_text(encoding="utf-8").strip()
                for path in sorted(directory.rglob("*"))
                if path.is_file() and path.suffix.lower() in _PROMPT_SUFFIXES
            ]
            contents = [content for content in contents if content]
            if contents:
                result.update({prompt_type: contents})
        return result

    @staticmethod
    def _load_subagents(root: Path) -> list[dict[str, str]]:
        """Expose ``subagents/*.yaml`` through the canonical ``SUBAGENTS`` configuration list."""
        directory = root / "subagents"
        if not directory.is_dir():
            return []
        return [
            {"path": str(path.resolve())}
            for path in sorted(directory.iterdir())
            if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}
        ]

    @staticmethod
    def _warn_ignored_legacy_content(suite: SuiteRecord) -> None:
        """Report retired Suite content that intentionally does not reach native config."""
        for relative_path, label in _UNSUPPORTED_SUITE_PATHS:
            if (suite.root / relative_path).exists():
                logger.warning(
                    "Suite '{}' {} at '{}' is ignored by the native runtime.", suite.name, label, relative_path
                )

    def _resolve_callable_spec(self, spec: str, suite: SuiteRecord, *, location: str) -> str | None:
        """Return a framework spec or register a Suite-local callable under a stable alias."""
        normalized = str(spec or "").strip()
        if not normalized:
            return normalized
        if normalized.startswith("dataagent.core.flex."):
            logger.warning("{}: retired Flex hook '{}' was ignored.", location, normalized)
            return None
        if normalized.startswith("dataagent.") or normalized.startswith("python:"):
            return normalized

        relative_spec = normalized.removeprefix(f"{suite.name}.")
        callback = import_callable_from_suite_root(relative_spec, root=suite.root, suite_name=suite.name)
        alias = self._callable_alias(suite, relative_spec)
        register_callable_spec(alias, callback)
        return alias

    @staticmethod
    def _callable_alias(suite: SuiteRecord, relative_spec: str) -> str:
        """Build a stable generic callable reference for one Suite-local Python object."""
        digest_input = f"{suite.root.resolve()}:{relative_spec}".encode()
        digest = hashlib.sha256(digest_input).hexdigest()[:16]
        callable_name = str(relative_spec).rsplit(".", maxsplit=1)[-1]
        return f"dataagent.suite_hooks.{suite.name}_{digest}.{callable_name}"

    @staticmethod
    def _merge_mapping(target: dict[str, Any], source: Mapping[str, Any]) -> None:
        """Merge independent bundle fragments into one Suite layer without list reordering."""
        for key, value in source.items():
            existing = target.get(key)
            if isinstance(existing, Mapping) and isinstance(value, Mapping):
                merged = dict(existing)
                SuiteConfigResolver._merge_mapping(merged, value)
                target.update({key: merged})
            else:
                target.update({key: value})
