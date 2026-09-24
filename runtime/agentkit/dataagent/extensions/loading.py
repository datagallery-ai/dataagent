"""Load extension resources from supplied locations: manifests, paths and Python objects.

Home discovery belongs to bootstrap. Python modules are cached for one compilation,
shared by tools and Hooks; this module does not execute Agent tasks.
"""

import hashlib
import importlib.util
import sys
from pathlib import Path

from dataagent.declarations import PluginSpec
from dataagent.settings import PluginSettings
from dataagent.strict_json import read_json


def contained_path(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute():
        raise ValueError("Plugin resource paths must be relative to the plugin directory")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Plugin resource escapes its root directory")
    return path


def select_plugins(settings: PluginSettings, plugin_roots=()) -> list[tuple[Path, PluginSpec]]:
    """Resolve `plugins.enabled` against candidate plugin directories, in enabled order.

    `plugin_roots` are plugin directories (builtin, user) discovered by bootstrap;
    `settings.paths` are explicitly configured plugin directories. The same real
    directory counts once; one ID matching two directories is a conflict, never a priority.
    """
    directories = list(dict.fromkeys(
        path.resolve(strict=True) for path in (*plugin_roots, *settings.paths)
    ))
    selected = []
    for plugin_id in settings.enabled:
        matches = [path for path in directories if path.name == plugin_id]
        if len(matches) != 1:
            locations = ", ".join(map(str, matches)) or "not found"
            raise ValueError(f"Plugin {plugin_id!r} must resolve to exactly one directory: {locations}")
        root = matches[0].resolve()
        spec = PluginSpec.model_validate(read_json(contained_path(root, ".plugin.json")))
        if spec.id != plugin_id:
            raise ValueError(f"Plugin directory and manifest ID disagree: {plugin_id}")
        selected.append((root, spec))
    return selected


class PythonLoader:
    """Import `file.py:symbol` entrypoints confined to a root; one module object per file."""

    def __init__(self):
        self.modules = {}

    def load(self, root: Path, entrypoint: str):
        path_text, separator, symbol = entrypoint.partition(":")
        if not separator or not symbol.isidentifier():
            raise ValueError("Expected a plugin-relative file.py:export entrypoint")
        path = contained_path(root, path_text)
        if path.suffix != ".py":
            raise ValueError("Only Python plugin entrypoints are supported")
        if path not in self.modules:
            name = "_dataagent_v2_plugin_" + hashlib.sha256(str(path).encode()).hexdigest()
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise ValueError(f"Cannot load Python entrypoint: {path.name}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            # Read source directly: same-second, same-size edits must not reuse stale .pyc.
            exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
            self.modules[path] = module
        return getattr(self.modules[path], symbol)
