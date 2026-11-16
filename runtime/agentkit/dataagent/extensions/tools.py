"""Load named plugin tools and resolve declarative SubAgent tool references."""

import re
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from dataagent.declarations import LOCAL_ID_PATTERN, PluginSpec
from dataagent.extensions.loading import PythonLoader

NATIVE_TOOL_NAMES = frozenset({
    "ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "task", "write_todos",
})


def collect_tools(
    root: Path, spec: PluginSpec, loader: PythonLoader, tool_registry: dict[str, BaseTool],
) -> None:
    for local_id, entry in spec.tools.items():
        if not re.fullmatch(LOCAL_ID_PATTERN, local_id):
            raise ValueError(f"Invalid local tool ID: {local_id}")
        canonical = f"{spec.id}__{local_id}"
        if canonical in tool_registry:
            raise ValueError(f"Duplicate tool: {canonical}")
        loaded = loader.load(root, entry.entrypoint)
        instance = loaded if isinstance(loaded, BaseTool) else tool(loaded)
        tool_registry[canonical] = instance.model_copy(update={"name": canonical})


def resolve_tool_refs(
    names: list[str], plugin_id: str, tool_registry: dict[str, BaseTool], *, agent_name: str,
) -> list[str]:
    ids = [name if "__" in name else f"{plugin_id}__{name}" for name in names]
    if len(set(ids)) != len(ids) or any(name not in tool_registry for name in ids):
        raise ValueError(f"Invalid tool references for SubAgent {agent_name}")
    return ids
