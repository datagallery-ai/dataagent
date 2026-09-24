"""Compile Skill sources using the pinned native metadata parser."""

from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, NotRequired

from deepagents.middleware.skills import SkillsMiddleware, SkillsState, _parse_skill_metadata
from langchain.agents.middleware.types import PrivateStateAttr

from dataagent.extensions.loading import contained_path


class VersionedSkillsState(SkillsState):
    extension_revision: NotRequired[Annotated[str, PrivateStateAttr]]


class VersionedSkillsMiddleware(SkillsMiddleware):
    """Refresh only derived Skill metadata when a host installs a new extension revision."""

    state_schema = VersionedSkillsState

    def __init__(self, *, backend, sources, revision: str):
        super().__init__(backend=backend, sources=sources)
        self.revision = revision

    @property
    def name(self):
        # Replace the native Skill slot, rather than installing a second scanner/prompt.
        return "SkillsMiddleware"

    def _needs_refresh(self, state):
        return state.get("extension_revision") != self.revision or "skills_metadata" not in state

    def before_agent(self, state, runtime, config):
        if not self._needs_refresh(state):
            return None
        fresh = {key: value for key, value in state.items() if key != "skills_metadata"}
        return {"skills_load_errors": [], **super().before_agent(fresh, runtime, config),
                "extension_revision": self.revision}

    async def abefore_agent(self, state, runtime, config):
        if not self._needs_refresh(state):
            return None
        fresh = {key: value for key, value in state.items() if key != "skills_metadata"}
        return {"skills_load_errors": [], **await super().abefore_agent(fresh, runtime, config),
                "extension_revision": self.revision}


def scan_skill_directory(
    source: Path, names: dict[str, Path], *, root: Path | None = None,
) -> bool:
    if not source.is_dir():
        raise ValueError(f"Skill source must be a directory: {source}")
    found = False
    for child in sorted(source.iterdir()):
        path = child / "SKILL.md"
        if not child.is_dir() or not (path.exists() or path.is_symlink()):
            continue
        if not path.is_file():
            raise ValueError(f"SKILL.md must be a regular file: {path}")
        real = contained_path(root, str(path.relative_to(root))) if root else path.resolve()
        metadata = _parse_skill_metadata(real.read_text(encoding="utf-8"), str(path), child.name)
        if metadata is None:
            raise ValueError(f"Invalid Skill metadata: {path}")
        name = metadata["name"]
        if name in names and names[name] != real:
            raise ValueError(f"Duplicate Skill name: {name}: {names[name]}, {real}")
        names[name] = real
        found = True
    return found


def compile_skill_sources(
    root: Path, paths: list[str], names: dict[str, Path] | None = None,
    *, label: str,
) -> list[tuple[str, str]]:
    # Reuse the parser from the pinned Deep Agents release: a configured Skill
    # must not be silently skipped by the native discovery middleware.
    names = {} if names is None else names
    sources = []
    for relative in paths:
        source = contained_path(root, relative)
        if not scan_skill_directory(source, names, root=root):
            raise ValueError(f"Skill source contains no SKILL.md files: {relative}")
        sources.append((str(source), label))
    return sources


def collect_loose_skills(
    sources: Iterable[tuple[str, str]], names: dict[str, Path],
) -> list[tuple[str, str]]:
    result = []
    for path, label in sources:
        if scan_skill_directory(Path(path), names):
            result.append((path, label))
    return result
