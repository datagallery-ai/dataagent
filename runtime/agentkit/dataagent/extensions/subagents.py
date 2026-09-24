"""Translate declarative SubAgents; native host policies are added later by agent.py."""

from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from dataagent.declarations import PluginSpec, SubAgentSpec
from dataagent.extensions.hooks import compile_hooks
from dataagent.extensions.loading import PythonLoader, contained_path
from dataagent.extensions.skills import compile_skill_sources
from dataagent.extensions.tools import resolve_tool_refs
from dataagent.strict_json import read_json


def collect_subagents(
    root: Path, spec: PluginSpec, child_specs: list[tuple[Path, str, SubAgentSpec]],
    agent_names: set[str],
) -> None:
    for relative in spec.subagents:
        child = SubAgentSpec.model_validate(read_json(contained_path(root, relative)))
        if child.name in agent_names:
            raise ValueError(f"Duplicate or reserved SubAgent name: {child.name}")
        agent_names.add(child.name)
        child_specs.append((root, spec.id, child))


def materialize_subagents(
    child_specs: list[tuple[Path, str, SubAgentSpec]], *, tool_registry: dict[str, BaseTool],
    loader: PythonLoader, skill_sources: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    children = []
    for root, plugin_id, spec in child_specs:
        ids = (resolve_tool_refs(spec.tools, plugin_id, tool_registry, agent_name=spec.name)
               if spec.tools is not None else list(tool_registry))
        compiled_hooks = compile_hooks([(root, hook) for hook in spec.hooks], loader, agent_name=spec.name)
        child = {
            "name": spec.name,
            "description": spec.description,
            "system_prompt": contained_path(root, spec.system_prompt).read_text(encoding="utf-8"),
            "skills": (list(skill_sources) if spec.skills is None
                       else compile_skill_sources(root, spec.skills, label=plugin_id)),
            "middleware": compiled_hooks,
        }
        # Upstream distinguishes omitted tools (inherit) from [] (no explicit tools).
        if spec.tools is not None:
            child["tools"] = [tool_registry[name] for name in ids]
        children.append(child)
    return children
