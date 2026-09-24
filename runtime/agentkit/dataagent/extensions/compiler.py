"""Selected declarations to native Deep Agents kwargs, without host policy."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from dataagent.declarations import HookSpec, PluginSpec, SubAgentSpec
from dataagent.extensions.hooks import compile_hooks
from dataagent.extensions.loading import PythonLoader, contained_path
from dataagent.extensions.skills import collect_loose_skills, compile_skill_sources
from dataagent.extensions.subagents import collect_subagents, materialize_subagents
from dataagent.extensions.tools import collect_tools

ROOT_AGENT_NAME = "dataagent-v2"


def compile_extensions(
    selected_plugins: list[tuple[Path, PluginSpec]], *,
    skill_sources: Sequence[tuple[str, str]] = (),
    hook_entries: Sequence[tuple[Path, HookSpec]] = (),
    mcp_tools: Sequence[BaseTool] = (),
) -> dict[str, Any]:
    """Combine plugins and standalone resources without adding host execution policy."""
    loader = PythonLoader()
    tool_registry: dict[str, BaseTool] = {}
    for tool in mcp_tools:
        if tool.name in tool_registry:
            raise ValueError(f"Duplicate MCP tool: {tool.name}")
        tool_registry[tool.name] = tool
    skill_names: dict[str, Path] = {}
    compiled_skill_sources: list[tuple[str, str]] = []
    all_hook_entries: list[tuple[Path, HookSpec]] = []
    child_specs: list[tuple[Path, str, SubAgentSpec]] = []
    agent_names = {ROOT_AGENT_NAME}
    plugin_ids: set[str] = set()
    prompts = []
    for root, spec in selected_plugins:
        if spec.id in plugin_ids:
            raise ValueError(f"Duplicate plugin ID: {spec.id}")
        plugin_ids.add(spec.id)
        if spec.system_prompt:
            prompts.append(contained_path(root, spec.system_prompt).read_text(encoding="utf-8"))
        compiled_skill_sources.extend(compile_skill_sources(root, spec.skills, skill_names, label=spec.id))
        collect_tools(root, spec, loader, tool_registry)
        all_hook_entries.extend((root, entry) for entry in spec.hooks)
        collect_subagents(root, spec, child_specs, agent_names)
    compiled_skill_sources.extend(collect_loose_skills(skill_sources, skill_names))
    compiled_skill_sources = list(dict.fromkeys(compiled_skill_sources))
    all_hook_entries.extend(hook_entries)

    compiled_hooks = compile_hooks(all_hook_entries, loader, agent_name=ROOT_AGENT_NAME)
    children = materialize_subagents(
        child_specs, tool_registry=tool_registry, loader=loader,
        skill_sources=compiled_skill_sources,
    )
    return {
        "name": ROOT_AGENT_NAME,
        "system_prompt": "\n\n".join(prompts),
        "tools": list(tool_registry.values()),
        "skills": compiled_skill_sources,
        "subagents": children,
        "middleware": compiled_hooks,  # Deep Agents' native parameter, not another extension kind.
    }
