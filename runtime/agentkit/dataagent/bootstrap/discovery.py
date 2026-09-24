"""Locate Home and builtin extensions without loading their Python code.

After configuration merging, locate plugins/Skills and bind each Hook to its source.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from dataagent.bootstrap.config_files import Layer
from dataagent.bootstrap.paths import RuntimePaths
from dataagent.declarations import HookSpec
from dataagent.settings import Settings

_IGNORED_RESOURCES = {"__pycache__", ".git", ".venv", "node_modules", ".pytest_cache", ".ruff_cache"}


def extension_revision(runtime) -> str:
    """Fingerprint extension sources, never business inputs or generated session state.

    Directory membership and nanosecond mtimes catch edits/additions/removals without
    reading large plugin assets every query. Directory symlink cycles are skipped.
    """
    home = runtime.paths.home
    roots = {home / "skills", home / ".mcp.json", home / ".env"}
    roots.update(item.path for item in runtime.report.configs)
    roots.update(item.path.with_name(".env") for item in runtime.report.configs)
    if runtime.report.env_file:
        roots.add(runtime.report.env_file)
    roots.update(root for root in (*runtime.extensions.plugin_roots, *runtime.settings.plugins.paths)
                 if root.name in runtime.settings.plugins.enabled)
    plugins = home / "plugins"
    if plugins.is_dir():
        roots.update(root for root in plugins.iterdir() if root.name in runtime.settings.plugins.enabled)
    roots.update(binding.base_dir / binding.spec.entrypoint.partition(":")[0]
                 for binding in runtime.extensions.hooks)
    digest = hashlib.sha256()

    def visit(path: Path, parents=frozenset()):
        digest.update(str(path).encode())
        if not path.exists():
            digest.update(b"missing")
            return
        stat = path.stat()
        real = path.resolve()
        digest.update(str(real).encode())
        if path.is_dir():
            if real in parents:
                return
            for child in sorted(path.iterdir()):
                if child.name not in _IGNORED_RESOURCES:
                    visit(child, parents | {real})
        else:
            digest.update(f"{stat.st_mtime_ns}:{stat.st_ctime_ns}:{stat.st_size}".encode())

    for root in sorted(roots):
        visit(root)
    return digest.hexdigest()


@dataclass(frozen=True)
class HookBinding:
    """One effective Hook declaration and its configuration directory; spec is not copied."""

    base_dir: Path
    spec: HookSpec = field(repr=False)


@dataclass(frozen=True)
class ExtensionLocations:
    """Plugin/Skill locations, source-bound Hooks and resolved native MCP connections.

    Independent Hooks accumulate across configuration layers. Bindings reference Settings'
    effective declarations and retain each one's directory. Plugin Hooks use plugin roots.
    MCP connection values are frozen and excluded from repr; diagnostics belong to StartupReport.
    """

    plugin_roots: tuple[Path, ...] = ()
    skill_sources: tuple[tuple[str, str], ...] = ()
    hooks: tuple[HookBinding, ...] = field(default=(), repr=False)
    mcp_servers: Mapping = field(default_factory=lambda: MappingProxyType({}), repr=False)


def locate_extensions(
    paths: RuntimePaths, layers: tuple[Layer, ...], settings: Settings,
) -> tuple[ExtensionLocations, tuple[tuple[str, tuple[str, ...]], ...]]:
    """Locate extensions and classify candidate plugin origins after configuration merging.

    Hooks come from the effective configuration, not from automatically importing hooks/.
    """
    roots: list[Path] = []
    categories: dict[Path, str] = {}
    skill_sources: list[tuple[str, str]] = []
    for scope, root, label in (("builtin", paths.builtin_plugins, None), ("user", paths.home, "User")):
        _collect_plugin_roots(scope, root, roots, categories)
        _collect_skill_source(root, label, skill_sources)
    origins = _plugin_origins(settings, roots, categories)

    locations = ExtensionLocations(
        plugin_roots=tuple(roots),
        skill_sources=tuple(skill_sources),
        hooks=_bind_hooks(layers, settings.dataagent.hooks),
    )
    return locations, origins


def _collect_plugin_roots(
    scope: str, root: Path, roots: list[Path], categories: dict[Path, str],
) -> None:
    plugins = root if scope == "builtin" else root / "plugins"
    for real in _plugin_directories(plugins):
        if real not in roots:
            roots.append(real)
            categories[real] = scope


def _plugin_directories(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [child.resolve() for child in sorted(root.iterdir())
            if child.is_dir() and not child.name.startswith(".")]


def _collect_skill_source(
    root: Path, label: str | None, skill_sources: list[tuple[str, str]],
) -> None:
    if label is not None and _has_skills(root / "skills"):
        source = str((root / "skills").resolve())
        if source not in {path for path, _ in skill_sources}:
            skill_sources.append((source, label))


def _has_skills(source: Path) -> bool:
    return source.is_dir() and any(
        child.is_dir() and (child / "SKILL.md").exists() for child in source.iterdir()
    )


def _plugin_origins(
    settings: Settings, roots: list[Path], categories: dict[Path, str],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    origins = []
    for plugin_id in settings.plugins.enabled:
        found = [categories[root] for root in roots if root.name == plugin_id]
        found.extend("explicit" for path in settings.plugins.paths if path.name == plugin_id)
        origins.append((plugin_id, tuple(dict.fromkeys(found))))
    return tuple(origins)


def _bind_hooks(layers: tuple[Layer, ...], hooks: tuple[HookSpec, ...]) -> tuple[HookBinding, ...]:
    """Follow merge's additive order; bind validated declarations without parsing them again."""
    base_dirs = (
        layer.path.parent.resolve()
        for layer in layers
        for _ in layer.data.get("dataagent", {}).get("hooks", ())
    )
    return tuple(HookBinding(base_dir, spec) for base_dir, spec in zip(base_dirs, hooks, strict=True))
