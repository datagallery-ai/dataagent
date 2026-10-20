"""Resolve local plugin dependencies and their configuration without importing plugin code."""

from pathlib import Path

from dataagent.declarations import PluginSpec
from dataagent.settings import PluginSettings
from dataagent.strict_json import read_json


def contained_path(root: Path, relative: str) -> Path:
    """Resolve a plugin resource while retaining its declaring directory boundary."""
    if Path(relative).is_absolute():
        raise ValueError("Plugin resource paths must be relative to the plugin directory")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Plugin resource escapes its root directory")
    return path


def resolve_plugins(settings: PluginSettings, plugin_roots=()) -> list[tuple[Path, PluginSpec]]:
    """Resolve dependency-first local plugins, rejecting missing, ambiguous and cyclic IDs."""
    directories = tuple(dict.fromkeys(path.resolve() for path in (*plugin_roots, *settings.paths)))
    selected = []
    complete = set()
    visiting = []

    def visit(plugin_id):
        if plugin_id in visiting:
            raise ValueError(f"Plugin dependency cycle: {' -> '.join([*visiting, plugin_id])}")
        if plugin_id in complete:
            return
        matches = [path for path in directories if path.name == plugin_id]
        if len(matches) != 1:
            locations = ", ".join(map(str, matches)) or "not found"
            raise ValueError(
                f"Plugin {plugin_id!r} must resolve to exactly one directory: {locations}"
            )
        root = matches[0]
        spec = PluginSpec.model_validate(read_json(contained_path(root, ".plugin.json")))
        if spec.id != plugin_id:
            raise ValueError(f"Plugin directory and manifest ID disagree: {plugin_id}")
        visiting.append(plugin_id)
        for dependency in spec.requires:
            visit(dependency)
        visiting.pop()
        complete.add(plugin_id)
        selected.append((root, spec))

    for plugin_id in settings.enabled:
        visit(plugin_id)
    return selected
