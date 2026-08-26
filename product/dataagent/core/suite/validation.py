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
"""Post-merge configuration validation (strict duplicates and constraints)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from loguru import logger

from dataagent.core.flex.hooks.registry import BUILTIN_HOOK_REGISTRY
from dataagent.core.managers.action_manager.manager import ToolManager
from dataagent.core.suite.allow_paths import effective_workspace_allow_paths
from dataagent.core.suite.merge import WORKFLOW_TOP_KEYS
from dataagent.governance import validate_governance_config
from dataagent.utils.constants import DEFAULT_BUILTIN_SKILL_NAMES
from dataagent.utils.runtime_paths import dataagent_package_path, resolve_effective_workspace_root

WORKFLOW_KEYS = WORKFLOW_TOP_KEYS


def validate_merged_config(
    result: Mapping[str, Any],
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    activated_suites: Sequence[Mapping[str, str]] | None = None,
) -> None:
    """
    Run post-merge validation on ``result`` (strict duplicates, skill names, subagent paths).

    Args:
        result: Merged configuration before assignment to ``ConfigManager.settings``.
        session_id: Optional session for workspace resolution warnings.
        user_id: Optional user for workspace resolution warnings.
        activated_suites: Optional activated Suite metadata for subagent allow roots.

    Raises:
        ValueError: Strict duplicate or forbidden configuration detected.
    """
    validate_strict_duplicates(result)
    validate_governance_config(result.get("GOVERNANCE"))
    validate_unique_skill_names(result)
    _validate_no_explicit_sub_agent_tool(result)
    _validate_subagent_paths(
        result,
        session_id=session_id,
        user_id=user_id,
        activated_suites=activated_suites,
    )


def validate_strict_duplicates(result: Mapping[str, Any]) -> None:
    """Reject duplicate keys in append-merge list sections."""
    tools = result.get("TOOLS")
    if isinstance(tools, Mapping):
        _check_duplicate_list_keys(
            tools.get("local_functions"),
            label="TOOLS.local_functions",
            key_fn=_local_function_key,
        )
        _check_duplicate_list_keys(
            tools.get("mcp_servers"),
            label="TOOLS.mcp_servers",
            key_fn=_mcp_server_key,
        )
        _check_duplicate_list_keys(
            tools.get("A2A"),
            label="TOOLS.A2A",
            key_fn=_a2a_key,
        )
        skills = tools.get("skills")
        if isinstance(skills, Mapping):
            _check_duplicate_list_keys(
                skills.get("builtin"),
                label="TOOLS.skills.builtin",
                key_fn=lambda item: str(item).strip(),
            )
            _check_duplicate_list_keys(
                skills.get("custom_dirs"),
                label="TOOLS.skills.custom_dirs",
                key_fn=lambda item: str(item).strip(),
            )

    hooks = result.get("HOOKS")
    if isinstance(hooks, Mapping):
        _validate_hook_duplicates(hooks)

    subagents = result.get("SUBAGENT_CONFIGS")
    if isinstance(subagents, Sequence) and not isinstance(subagents, (str, bytes)):
        _check_duplicate_list_keys(subagents, label="SUBAGENT_CONFIGS.path", key_fn=_subagent_path_key)
        _check_duplicate_list_keys(
            subagents,
            label="SUBAGENT_CONFIGS.AGENT_CONFIG.name",
            key_fn=_subagent_name_key,
        )

    resources = result.get("RESOURCES")
    if isinstance(resources, Sequence) and not isinstance(resources, (str, bytes)):
        _check_duplicate_list_keys(resources, label="RESOURCES.id", key_fn=_resource_id_key)
        from dataagent.resources import validate_resources_list

        validate_resources_list(list(resources))

    for workflow_key in WORKFLOW_KEYS:
        nodes = result.get(workflow_key)
        if isinstance(nodes, Sequence) and not isinstance(nodes, (str, bytes)):
            _check_duplicate_list_keys(nodes, label=workflow_key, key_fn=_workflow_node_key)


def _check_duplicate_list_keys(
    items: Any,
    *,
    label: str,
    key_fn,
) -> None:
    """Raise when ``key_fn(item)`` repeats within ``items``."""
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return
    seen: set[str] = set()
    for item in items:
        try:
            key = key_fn(item)
        except ValueError as exc:
            raise ValueError(f"{label}: {exc}") from exc
        if not key:
            continue
        if key in seen:
            raise ValueError(f"Duplicate {label} entry: {key!r}")
        seen.add(key)


def _local_function_key(item: Any) -> str:
    if not isinstance(item, Mapping):
        return ""
    fn = item.get("function") or item.get("name")
    return str(fn or "").strip()


def _mcp_server_key(item: Any) -> str:
    if not isinstance(item, Mapping):
        return ""
    return str(item.get("server_id") or "").strip()


def _a2a_key(item: Any) -> str:
    if not isinstance(item, Mapping):
        return ""
    if len(item) == 1:
        return str(next(iter(item.keys()))).strip()
    return str(item.get("agent_id") or item.get("name") or "").strip()


def _subagent_path_key(item: Any) -> str:
    if not isinstance(item, Mapping):
        return ""
    raw = item.get("path")
    if raw is None:
        return ""
    path = ToolManager.resolve_subagent_config_path(raw)
    ToolManager.load_subagent_catalog_metadata(path)
    return str(path)


def _subagent_name_key(item: Any) -> str:
    if not isinstance(item, Mapping):
        return ""
    raw = item.get("path")
    if raw is None:
        return ""
    path = ToolManager.resolve_subagent_config_path(raw)
    name, _ = ToolManager.load_subagent_catalog_metadata(path)
    return name


def _resource_id_key(item: Any) -> str:
    if not isinstance(item, Mapping):
        return ""
    return str(item.get("id") or "").strip()


def _workflow_node_key(item: Any) -> str:
    if not isinstance(item, Mapping):
        return ""
    return str(item.get("node") or "").strip()


def _validate_hook_duplicates(hooks: Mapping[str, Any]) -> None:
    """Check duplicate normalized hook paths per HOOKS slot list."""
    for slot_path, items in _iter_hook_slot_lists(hooks, prefix=()):
        _check_duplicate_list_keys(
            items,
            label=".".join(("HOOKS",) + slot_path),
            key_fn=_normalize_hook_item,
        )


def _iter_hook_slot_lists(node: Any, *, prefix: tuple[str, ...]):
    """Yield (slot_path, hook_list) for leaf lists under HOOKS."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            child_prefix = prefix + (str(key),)
            yield from _iter_hook_slot_lists(value, prefix=child_prefix)
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)) and prefix:
        yield prefix, node


def _normalize_hook_item(item: Any) -> str:
    """Normalize a hook entry to a comparable full-path string for duplicate checks."""
    if isinstance(item, str):
        spec = item.strip()
    elif isinstance(item, Mapping):
        spec = str(item.get("name") or "").strip()
    else:
        return ""
    if not spec:
        return ""
    return BUILTIN_HOOK_REGISTRY.get(spec, spec)


def validate_unique_skill_names(result: Mapping[str, Any]) -> None:
    """
    Ensure effective skill ``name`` values are globally unique across load sources.

    Collects names from package ``actions/skills/`` (allowlist-gated, same as runtime) and
    every merged ``TOOLS.skills.custom_dirs`` root (full scan). Duplicate ``name`` with
    different paths → ``ValueError`` (reload blocked).
    """
    tools = result.get("TOOLS")
    if not isinstance(tools, Mapping):
        return

    registrations: dict[str, list[tuple[str, str]]] = {}

    def _register(name: str, source: str, path: str) -> None:
        registrations.setdefault(name, []).append((source, path))

    tools_mapping = dict(tools)
    builtin_allowlist = set(ToolManager.extract_skill_allowlist(tools_mapping, "builtin"))
    builtin_allowlist.update(DEFAULT_BUILTIN_SKILL_NAMES)

    default_root = dataagent_package_path("actions", "skills")
    package_skills, _ = ToolManager.discover_skills_from_root(
        root=default_root,
        allowlist=builtin_allowlist,
    )
    for skill in package_skills:
        _register(skill["name"], "actions/skills (allowlist)", skill["path"])

    for raw_path in ToolManager.extract_skill_directory_paths(tools_mapping):
        if raw_path == "actions/skills":
            continue
        root = Path(raw_path) if Path(raw_path).is_absolute() else dataagent_package_path(*str(raw_path).split("/"))
        source_label = f"TOOLS.skills.custom_dirs ({root})"
        extra_skills, _ = ToolManager.discover_skills_from_root(root=root, allowlist=None)
        for skill in extra_skills:
            _register(skill["name"], source_label, skill["path"])

    for name, entries in registrations.items():
        unique_paths = {path for _, path in entries}
        if len(unique_paths) <= 1:
            continue
        lines = [f"Duplicate skill name {name!r}:"]
        for source, path in entries:
            lines.append(f"  - {source}: {path}")
        raise ValueError("\n".join(lines))


def _validate_no_explicit_sub_agent_tool(result: Mapping[str, Any]) -> None:
    """Reject explicit ``sub_agent_tool`` in TOOLS.local_functions."""
    tools = result.get("TOOLS")
    if not isinstance(tools, Mapping):
        return
    local_functions = tools.get("local_functions")
    if not isinstance(local_functions, Sequence) or isinstance(local_functions, (str, bytes)):
        return
    for item in local_functions:
        if isinstance(item, Mapping) and ToolManager.is_explicit_sub_agent_tool_entry(item):
            raise ValueError("TOOLS.local_functions must not declare sub_agent_tool; use SUBAGENT_CONFIGS instead.")


def _validate_subagent_paths(
    result: Mapping[str, Any],
    *,
    session_id: str | None,
    user_id: str | None,
    activated_suites: Sequence[Mapping[str, str]] | None = None,
) -> None:
    """Warn when SUBAGENT_CONFIGS paths fall outside workspace / effective allow roots."""
    entries = result.get("SUBAGENT_CONFIGS")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return
    workspace_root = resolve_effective_workspace_root(config=result, session_id=session_id, user_id=user_id)
    allow_paths: list[Path] = [workspace_root]
    for item in effective_workspace_allow_paths(result, activated_suites):
        if str(item).strip():
            allow_paths.append(Path(str(item)).expanduser().resolve())

    def _allowed(path: Path) -> bool:
        resolved = path.resolve()
        for root in allow_paths:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        try:
            path = ToolManager.resolve_subagent_config_path(entry.get("path"))
        except ValueError:
            continue
        if not _allowed(path):
            logger.warning(
                "SUBAGENT_CONFIGS path {} is outside effective read roots "
                "(workspace, WORKSPACE.allow_path, activated suite roots); "
                "runtime sandbox may reject sub_agent_tool calls",
                path,
            )
