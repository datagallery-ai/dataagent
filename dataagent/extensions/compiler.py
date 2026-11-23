"""Selected declarations to native Deep Agents kwargs, without host policy."""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from dataagent.config.declarations import HookSpec, PluginSpec, SubAgentSpec
from dataagent.extensions.hooks import compile_hooks
from dataagent.extensions.loading import PythonLoader, contained_path
from dataagent.extensions.skills import collect_loose_skills, compile_skill_sources
from dataagent.extensions.subagents import collect_subagents, materialize_subagents
from dataagent.extensions.tools import collect_tools
from dataagent.semantic_client import SemanticServiceClient

ROOT_AGENT_NAME = "dataagent"


def compile_extensions(
    selected_plugins: list[tuple[Path, PluginSpec]],
    *,
    skill_sources: Sequence[tuple[str, str]] = (),
    hook_entries: Sequence[tuple[Path, HookSpec]] = (),
    mcp_tools: Sequence[BaseTool] = (),
    a2a_tools: Sequence[BaseTool] = (),
    plugin_params: dict[str, dict[str, Any]] | None = None,
    model_resolver: Callable[[str], BaseChatModel] | None = None,
    semantic_client: SemanticServiceClient | None = None,
    outputs_dir: Path | None = None,
    context=None,
    run_hook_entries=None,
) -> dict[str, Any]:
    """Combine plugins and standalone resources without adding host execution policy.

    ``plugin_params`` (Agent configuration ``plugins.config``), ``model_resolver``,
    ``semantic_client`` (the host-owned semantic-service client) and ``outputs_dir``
    (the session output directory) reach compiled-subagent graph factories;
    standard declarations ignore them.
    """
    loader = PythonLoader()
    tool_registry: dict[str, BaseTool] = {}
    for tool in mcp_tools:
        if tool.name in tool_registry:
            raise ValueError(f"Duplicate MCP tool: {tool.name}")
        tool_registry[tool.name] = tool
    for tool in a2a_tools:
        if tool.name in tool_registry:
            raise ValueError(f"Duplicate A2A tool: {tool.name}")
        tool_registry[tool.name] = tool
    skill_names: dict[str, Path] = {}
    compiled_skill_sources: list[tuple[str, str]] = []
    all_hook_entries: list[tuple[Path, HookSpec]] = []
    child_specs: list[tuple[Path, str, SubAgentSpec]] = []
    agent_names = {ROOT_AGENT_NAME}
    plugin_ids: set[str] = set()
    child_only_tools: set[str] = set()
    prompts = []
    for root, spec in selected_plugins:
        if spec.id in plugin_ids:
            raise ValueError(f"Duplicate plugin ID: {spec.id}")
        plugin_ids.add(spec.id)
        if spec.system_prompt_file:
            prompts.append(
                contained_path(root, spec.system_prompt_file).read_text(encoding="utf-8")
            )
        compiled_skill_sources.extend(
            compile_skill_sources(root, spec.skills, skill_names, label=spec.id)
        )
        collect_tools(root, spec, loader, tool_registry)
        child_only_tools.update(
            f"{spec.id}__{name}" for name, entry in spec.tools.items() if entry.scope == "subagents"
        )
        all_hook_entries.extend((root, entry) for entry in spec.hooks)
        collect_subagents(root, spec, child_specs, agent_names)
    compiled_skill_sources.extend(collect_loose_skills(skill_sources, skill_names))
    compiled_skill_sources = list(dict.fromkeys(compiled_skill_sources))
    all_hook_entries.extend(hook_entries)

    compiled_hooks = compile_hooks(all_hook_entries, loader, agent_name=ROOT_AGENT_NAME)
    with_workflow = {plugin_id for _, plugin_id, child in child_specs if child.kind == "compiled"}
    for plugin_id in sorted(set(run_hook_entries or {}) & plugin_ids - with_workflow):
        raise ValueError(
            f"plugins.config.{plugin_id}.run_hooks needs a compiled SubAgent in that plugin; "
            "use dataagent.hooks or the plugin's hooks for Agent Hooks"
        )
    children = materialize_subagents(
        child_specs,
        tool_registry=tool_registry,
        loader=loader,
        skill_sources=compiled_skill_sources,
        plugin_params=plugin_params,
        model_resolver=model_resolver,
        semantic_client=semantic_client,
        context=context,
        run_hook_entries=run_hook_entries,
        outputs_dir=outputs_dir,
    )
    return {
        "name": ROOT_AGENT_NAME,
        "system_prompt": "\n\n".join(prompts),
        "tools": [tool for name, tool in tool_registry.items() if name not in child_only_tools],
        "skills": compiled_skill_sources,
        "subagents": children,
        "middleware": compiled_hooks,  # Deep Agents' native parameter, not another extension kind.
    }
