"""Translate declarative SubAgents; native host policies are added later by agent.py.

Standard declarations become prompt-driven agent children. Compiled
declarations (``kind="compiled"``) call the plugin's graph factory with its
parameters and host services, and pass the returned Runnable to the core as a
native ``CompiledSubAgent``; a factory may also return a description supplement.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

from dataagent.declarations import PluginSpec, SubAgentSpec
from dataagent.extensions.hooks import compile_hooks
from dataagent.extensions.loading import PythonLoader, contained_path
from dataagent.extensions.skills import compile_skill_sources
from dataagent.extensions.tools import resolve_tool_refs
from dataagent.semantic_client import SemanticServiceClient
from dataagent.strict_json import read_json


def collect_subagents(
    root: Path, spec: PluginSpec, child_specs: list[tuple[Path, str, SubAgentSpec]],
    agent_names: set[str],
) -> None:
    for relative in spec.subagents:
        child = SubAgentSpec.model_validate(read_json(contained_path(root, relative)))
        if child.description_file:
            text = contained_path(root, child.description_file).read_text(encoding="utf-8").strip()
            if not text:
                raise ValueError(f"SubAgent {child.name!r} description_file is empty: {child.description_file}")
            child = child.model_copy(update={"description": text})
        if child.name in agent_names:
            raise ValueError(f"Duplicate or reserved SubAgent name: {child.name}")
        agent_names.add(child.name)
        child_specs.append((root, spec.id, child))


@dataclass(frozen=True)
class SubagentServices:
    """Host services available to compiled-subagent factories.

    ``get_model`` resolves a model alias from the effective Agent configuration;
    factories call it for the aliases their parameters name. ``semantic_client``
    is the host-owned semantic-service client (None when no semantic service is
    configured); factories consume it without owning its lifecycle.
    ``outputs_dir`` is the current session's output directory (None outside a
    session), where factories may write result files the parent agent can read.
    """

    get_model: Callable[[str], BaseChatModel]
    semantic_client: SemanticServiceClient | None = None
    outputs_dir: Path | None = None


def _unresolvable_model(alias: str) -> BaseChatModel:
    raise ValueError(f"No models are configured; cannot resolve SubAgent model alias {alias!r}")


_NO_SERVICES = SubagentServices(get_model=_unresolvable_model)


def _compiled_child(
    root: Path, plugin_id: str, spec: SubAgentSpec, *, loader: PythonLoader,
    plugin_params: dict[str, dict[str, Any]] | None, services: SubagentServices,
) -> dict[str, Any]:
    params = dict((plugin_params or {}).get(plugin_id) or {})
    factory = loader.load(root, spec.entrypoint)
    if not callable(factory):
        raise TypeError(
            f"Compiled SubAgent entrypoint {spec.entrypoint!r} of plugin {plugin_id!r} "
            f"resolved to non-callable {type(factory).__name__}"
        )
    # A factory returns its Runnable, or {"runnable": ..., "description": ...} to
    # append runtime facts (e.g. the bound data source) to the declared description.
    result = factory(params, services)
    extra = None
    if isinstance(result, dict):
        unknown = sorted(set(result) - {"runnable", "description"})
        extra = result.get("description")
        if unknown or not isinstance(extra, (str, type(None))):
            raise TypeError(
                f"Compiled SubAgent factory for {spec.name!r} returned an invalid mapping: "
                "expected keys 'runnable' and optional string 'description'"
            )
        result = result.get("runnable")
    if not isinstance(result, Runnable):
        raise TypeError(
            f"Compiled SubAgent factory for {spec.name!r} must return a langchain Runnable, "
            f"got {type(result).__name__}"
        )
    description = f"{spec.description}\n{extra.strip()}" if extra and extra.strip() else spec.description
    return {"name": spec.name, "description": description, "runnable": result}


def materialize_subagents(
    child_specs: list[tuple[Path, str, SubAgentSpec]], *, tool_registry: dict[str, BaseTool],
    loader: PythonLoader, skill_sources: list[tuple[str, str]],
    plugin_params: dict[str, dict[str, Any]] | None = None,
    model_resolver: Callable[[str], BaseChatModel] | None = None,
    semantic_client: SemanticServiceClient | None = None,
    outputs_dir: Path | None = None,
) -> list[dict[str, Any]]:
    if model_resolver or semantic_client or outputs_dir:
        services = SubagentServices(
            get_model=model_resolver or _unresolvable_model, semantic_client=semantic_client,
            outputs_dir=outputs_dir,
        )
    else:
        services = _NO_SERVICES
    children = []
    for root, plugin_id, spec in child_specs:
        if spec.kind == "compiled":
            children.append(_compiled_child(
                root, plugin_id, spec, loader=loader,
                plugin_params=plugin_params, services=services,
            ))
            continue
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
