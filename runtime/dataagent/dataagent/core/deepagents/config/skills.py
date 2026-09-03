"""Compile legacy DataAgent skill settings into native Deep Agents sources."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.protocol import BackendProtocol, LsResult
from deepagents.middleware.filesystem import FilesystemPermission

from dataagent.utils.runtime_paths import dataagent_package_root, resolve_user_root

_SKILLS_ROOT = "/skills"


@dataclass(frozen=True)
class SkillConfig:
    """Native Deep Agents skill sources and their supporting backend."""

    sources: tuple[str, ...]
    backend: BackendProtocol
    permissions: tuple[FilesystemPermission, ...]


class SkillConfigCompiler:
    """Normalize compatible ``TOOLS.skills`` forms into Deep Agents skills."""

    def __init__(self, config: Mapping[str, Any], backend: BackendProtocol) -> None:
        self._config = config
        self._backend = backend
        self._package_root = dataagent_package_root()

    def compile(self) -> SkillConfig:
        """Compile builtin, custom, and per-user skills into native sources."""
        skills_config = self._skills_config()
        routes: dict[str, BackendProtocol] = {}
        sources: list[str] = []

        builtin_names = self._name_allowlist(skills_config, "builtin", default=())
        builtin_root = self._package_root / "actions" / "skills"
        self._add_filtered_source(routes, sources, "/skills/builtin/", builtin_root, builtin_names)

        for index, root in enumerate(self._custom_roots(skills_config)):
            self._add_directory_source(routes, sources, f"/skills/custom-{index}/", root)

        user_root = self._user_skills_root()
        if "user" in skills_config:
            user_names = self._name_allowlist(skills_config, "user", default=())
            self._add_filtered_source(routes, sources, "/skills/user/", user_root, user_names)
        else:
            self._add_directory_source(routes, sources, "/skills/user/", user_root)

        if not sources:
            return SkillConfig(sources=(), backend=self._backend, permissions=())

        backend = CompositeBackend(default=self._backend, routes=routes)
        permissions = (FilesystemPermission(operations=["write"], paths=[f"{_SKILLS_ROOT}/**"], mode="deny"),)
        return SkillConfig(sources=tuple(sources), backend=backend, permissions=permissions)

    def _skills_config(self) -> Mapping[str, Any]:
        tools_config = self._as_mapping(self._config.get("TOOLS", {}), "TOOLS")
        raw = tools_config.get("skills")
        if raw is None:
            return {}
        return self._as_mapping(raw, "TOOLS.skills")

    @staticmethod
    def _as_mapping(value: Any, path: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{path} must be a mapping.")
        return value

    @staticmethod
    def _name_allowlist(
        skills_config: Mapping[str, Any],
        key: str,
        *,
        default: Sequence[str],
    ) -> set[str]:
        raw = skills_config.get(key, default)
        if raw is None:
            return set()
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise ValueError(f"TOOLS.skills.{key} must be a list of skill names.")
        return {str(item).strip() for item in raw if str(item).strip()}

    def _custom_roots(self, skills_config: Mapping[str, Any]) -> tuple[Path, ...]:
        raw = skills_config.get("custom_dirs", ())
        if raw is None:
            return ()
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise ValueError("TOOLS.skills.custom_dirs must be a list of directory paths.")

        roots: list[Path] = []
        for item in raw:
            configured_path = str(item).strip()
            if not configured_path:
                continue
            path = Path(configured_path).expanduser()
            roots.append(path.resolve() if path.is_absolute() else (self._package_root / path).resolve())
        return tuple(roots)

    def _user_skills_root(self) -> Path:
        return (resolve_user_root(config=self._config) / "skills").resolve()

    @staticmethod
    def _add_directory_source(
        routes: dict[str, BackendProtocol],
        sources: list[str],
        source: str,
        root: Path,
    ) -> None:
        if not root.is_dir():
            return
        routes[source] = FilesystemBackend(root_dir=root, virtual_mode=True)
        sources.append(source)

    def _add_filtered_source(
        self,
        routes: dict[str, BackendProtocol],
        sources: list[str],
        source: str,
        root: Path,
        allowlist: set[str],
    ) -> None:
        if not allowlist or not root.is_dir():
            return

        allowed_directories: set[str] = set()
        for child in sorted(root.iterdir()):
            if not child.is_dir() or self._read_skill_name(child) not in allowlist:
                continue
            allowed_directories.add(child.name)

        if not allowed_directories:
            return
        routes[source] = _FilteredSkillSourceBackend(root, allowed_directories)
        sources.append(source)

    @staticmethod
    def _read_skill_name(skill_root: Path) -> str | None:
        skill_file = skill_root / "SKILL.md"
        if not skill_file.is_file():
            return None
        content = skill_file.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return None
        try:
            _, frontmatter, _ = content.split("---", 2)
            parsed = yaml.safe_load(frontmatter)
        except (ValueError, yaml.YAMLError):
            return None
        if not isinstance(parsed, Mapping):
            return None
        return str(parsed.get("name", "")).strip() or None


class _FilteredSkillSourceBackend(FilesystemBackend):
    """Expose only selected child skill directories from a filesystem root."""

    def __init__(self, root: Path, allowed_directories: set[str]) -> None:
        super().__init__(root_dir=root, virtual_mode=True)
        self._allowed_directories = frozenset(allowed_directories)

    def ls(self, path: str) -> LsResult:
        """List files while filtering the skill source root."""
        return self._filter_root(path, super().ls(path))

    async def als(self, path: str) -> LsResult:
        """Asynchronously list files while filtering the skill source root."""
        return self._filter_root(path, await super().als(path))

    def _filter_root(self, path: str, result: LsResult) -> LsResult:
        if PurePosixPath(path or "/") != PurePosixPath("/") or result.error:
            return result
        entries = [
            entry
            for entry in (result.entries or [])
            if entry.get("is_dir")
            and PurePosixPath(str(entry.get("path", "")).rstrip("/")).name in self._allowed_directories
        ]
        return LsResult(entries=entries)
