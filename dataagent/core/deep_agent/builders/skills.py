# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""OpenJiuWen SkillUseRail adapter with DataAgent-compatible sources."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dataagent.core.deep_agent.spec import SkillSpec
from dataagent.utils.runtime_paths import dataagent_package_path, resolve_user_root


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _resolve_custom_dirs(paths: list[str | Path] | tuple[str | Path, ...]) -> tuple[Path, ...]:
    resolved: list[Path] = []
    for raw_path in paths:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = dataagent_package_path(*candidate.parts)
        candidate = candidate.resolve()
        if candidate not in resolved:
            resolved.append(candidate)
    return tuple(resolved)


def _validate_skill_roots(roots: tuple[Path, ...]) -> None:
    for root in roots:
        if not root.is_dir():
            continue
        for skill_dir in root.iterdir():
            skill_md = skill_dir / "SKILL.md"
            if not skill_dir.is_dir() or not skill_md.is_file():
                continue
            content = skill_md.read_text(encoding="utf-8")
            if not content.startswith("---"):
                raise ValueError(f"{skill_md} must contain YAML frontmatter")
            parts = content.split("---", 2)
            metadata = yaml.safe_load(parts[1]) if len(parts) >= 3 else None
            if not isinstance(metadata, dict) or not str(metadata.get("description", "")).strip():
                raise ValueError(f"{skill_md} frontmatter requires a non-empty description")


@dataclass
class SkillRailBinding:
    """Mutable runtime binding around one Jiuwen SkillUseRail."""

    spec: SkillSpec
    rail: Any
    base_read_roots: tuple[Path, ...] = ()
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def roots(self) -> tuple[Path, ...]:
        return self.spec.roots

    async def refresh(
        self,
        *,
        user_id: str | None = None,
        custom_dirs: list[str | Path] | tuple[str | Path, ...] | None = None,
    ) -> list[Any]:
        """Refresh files, optionally replacing runtime user/custom roots."""
        async with self._lock:
            old_spec = self.spec
            old_skills_dir = self.rail.skills_dir
            old_builtin_root = self.rail.builtin_root
            old_builtin_allowlist = self.rail.builtin_allowlist
            old_skill_cache = dict(self.rail._skill_cache)
            old_skill_update_at = dict(self.rail._skill_update_at)
            old_skill_order = list(self.rail._skill_order)
            old_skills = list(self.rail.skills)
            run_config = getattr(self.rail.sys_operation, "_run_config", None)
            old_sandbox_root = (
                list(getattr(run_config, "sandbox_root", None) or [])
                if run_config is not None
                else None
            )
            next_spec = SkillSpec(
                builtin_root=self.spec.builtin_root,
                builtin_allowlist=self.spec.builtin_allowlist,
                custom_dirs=(
                    _resolve_custom_dirs(custom_dirs)
                    if custom_dirs is not None
                    else self.spec.custom_dirs
                ),
                user_root=(
                    resolve_user_root(user_id=user_id) / "skills"
                    if user_id is not None
                    else self.spec.user_root
                ),
            )
            _validate_skill_roots(next_spec.roots)
            self.rail.skills_dir = [str(path) for path in next_spec.roots]
            self.rail.builtin_root = next_spec.builtin_root
            self.rail.builtin_allowlist = next_spec.builtin_allowlist
            if run_config is not None:
                run_config.sandbox_root = [
                    str(path)
                    for path in dict.fromkeys((*self.base_read_roots, *next_spec.roots))
                ]
            try:
                await self.rail.reload_skills()
            except Exception:
                self.rail.skills_dir = old_skills_dir
                self.rail.builtin_root = old_builtin_root
                self.rail.builtin_allowlist = old_builtin_allowlist
                self.rail._skill_cache = old_skill_cache
                self.rail._skill_update_at = old_skill_update_at
                self.rail._skill_order = old_skill_order
                self.rail.skills = old_skills
                if run_config is not None:
                    run_config.sandbox_root = old_sandbox_root
                self.spec = old_spec
                raise
            self.spec = next_spec
            return self.rail.skills_meta


def build_skill_rail(
    spec: SkillSpec | None,
    *,
    sys_operation: Any | None = None,
    base_read_roots: tuple[Path, ...] = (),
) -> SkillRailBinding | None:
    """Build the explicit SkillUseRail consumed by ``create_deep_agent``."""
    if spec is None:
        return None

    from openjiuwen.harness.rails.skills import SkillUseRail

    class DataAgentSkillUseRail(SkillUseRail):
        def __init__(self) -> None:
            self._dedicated_read_sys_operation = None
            self.builtin_root = spec.builtin_root
            self.builtin_allowlist = spec.builtin_allowlist
            super().__init__(
                skills_dir=[str(path) for path in spec.roots],
                skill_mode="all",
                enable_cache=True,
                include_tools=False,
            )

        def set_sys_operation(self, operation):
            """Keep the dedicated read-only operation across DeepAgent init."""
            if self._dedicated_read_sys_operation is None:
                self._dedicated_read_sys_operation = operation
            super().set_sys_operation(self._dedicated_read_sys_operation)

        def _filter_skills(self, skills):
            filtered = [
                skill
                for skill in skills
                if not _is_relative_to(skill.directory, self.builtin_root)
                or skill.name in self.builtin_allowlist
            ]
            return super()._filter_skills(filtered)

    rail = DataAgentSkillUseRail()
    if sys_operation is not None:
        rail.set_sys_operation(sys_operation)
    return SkillRailBinding(
        spec=spec,
        rail=rail,
        base_read_roots=base_read_roots,
    )
