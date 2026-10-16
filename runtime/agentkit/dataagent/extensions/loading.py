"""Load extension resources from supplied locations: manifests, paths and Python objects.

Home discovery belongs to bootstrap. Python modules are cached for one compilation,
shared by tools and Hooks; this module does not execute Agent tasks.
"""

import hashlib
import importlib.util
import sys
from itertools import count
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


_PLUGIN_NAMESPACE = "_dataagent_v2_plugin_"
_loader_ids = count()


class PythonLoader:
    """Import `file.py:symbol` entrypoints confined to a root; one module object per file.

    Each plugin directory loads inside one namespace per compilation: the same
    internal package name in two plugins never collides, and a new compilation
    executes fresh code while graphs built by earlier compilations keep working
    through their own module references. The plugin root stays importable while
    an entrypoint executes, so factory files can import their sibling plugin
    packages (``from my_plugin.graph import ...``) without modifying global
    ``sys.path`` themselves: afterwards the path entry and the plugin's plain
    module names are gone; only the namespaced modules remain, so internal
    imports must be eager.
    """

    def __init__(self):
        self.modules = {}
        self._namespaces = {}

    def _namespace(self, root: Path) -> str:
        """One namespace per plugin root per loader; recompiling gets a fresh module graph."""
        key = root.resolve()
        if key not in self._namespaces:
            self._namespaces[key] = (
                f"{_PLUGIN_NAMESPACE}{next(_loader_ids)}_"
                f"{hashlib.sha256(str(key).encode()).hexdigest()}"
            )
        return self._namespaces[key]

    def load(self, root: Path, entrypoint: str):
        path_text, separator, symbol = entrypoint.partition(":")
        if not separator or not symbol.isidentifier():
            raise ValueError("Expected a plugin-relative file.py:export entrypoint")
        path = contained_path(root, path_text)
        if path.suffix != ".py":
            raise ValueError("Only Python plugin entrypoints are supported")
        if path not in self.modules:
            self.modules[path] = self._exec_module(root, path)
        return getattr(self.modules[path], symbol)

    def _exec_module(self, root: Path, path: Path):
        namespace = self._namespace(root)
        name = f"{namespace}._entry_{hashlib.sha256(str(path).encode()).hexdigest()}"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ValueError(f"Cannot load Python entrypoint: {path.name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        prefix = namespace + "."
        # Earlier entrypoints of this plugin already executed in this
        # compilation: expose their namespaced modules under plain names so
        # this entrypoint attaches submodules to those same package objects
        # instead of executing second copies.
        exposed = [
            key for key in sys.modules
            if key.startswith(prefix) and not key[len(prefix):].startswith("_entry_")
            and key[len(prefix):] not in sys.modules
        ]
        for key in exposed:
            sys.modules[key[len(prefix):]] = sys.modules[key]
        before = set(sys.modules)
        # Read source directly: same-second, same-size edits must not reuse stale .pyc.
        root_text = str(root)
        resolved_root = root.resolve()
        added = root_text not in sys.path
        if added:
            sys.path.insert(0, root_text)
        try:
            exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        finally:
            if added and root_text in sys.path:
                sys.path.remove(root_text)
            for key in exposed:
                sys.modules.pop(key[len(prefix):], None)
            # Modules this entrypoint imported from the plugin directory move
            # into the plugin namespace; host and third-party imports stay put.
            for key in list(sys.modules):
                if key in before or key.startswith(_PLUGIN_NAMESPACE):
                    continue
                origin = getattr(sys.modules[key], "__file__", None)
                if origin and Path(origin).resolve().is_relative_to(resolved_root):
                    sys.modules[prefix + key] = sys.modules.pop(key)
        return module
