# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Workspace read/write access policy and Jiuwen SysOperation construction."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dataagent.core.deep_agent.spec import SkillSpec

_DISABLED_SHELL_ALLOWLIST = ["__dataagent_bash_disabled__"]


def _canonical(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _dedupe(paths: Sequence[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    for path in paths:
        if path not in result:
            result.append(path)
    return tuple(result)


@dataclass(frozen=True)
class WorkspaceAccessPolicy:
    """Canonical roots for controlled read and write operations."""

    workspace_root: Path
    allow_read_roots: tuple[Path, ...] = ()
    skill_roots: tuple[Path, ...] = ()

    @property
    def write_roots(self) -> tuple[Path, ...]:
        return (self.workspace_root,)

    @property
    def read_roots(self) -> tuple[Path, ...]:
        return _dedupe((self.workspace_root, *self.allow_read_roots, *self.skill_roots))

    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        workspace_root: str | Path,
        skills: SkillSpec | None = None,
    ) -> WorkspaceAccessPolicy:
        settings = config.get_all() if hasattr(config, "get_all") else config
        workspace_section = settings.get("WORKSPACE", {}) if isinstance(settings, Mapping) else {}
        raw_allow_paths = workspace_section.get("allow_path") if isinstance(workspace_section, Mapping) else None

        if raw_allow_paths is None:
            allow_paths: tuple[Path, ...] = ()
        else:
            if isinstance(raw_allow_paths, (str, bytes)) or not isinstance(raw_allow_paths, Sequence):
                raise ValueError("WORKSPACE.allow_path must be a list of absolute path strings.")
            normalized: list[Path] = []
            for index, raw_path in enumerate(raw_allow_paths):
                value = str(raw_path).strip()
                if not value:
                    raise ValueError(f"WORKSPACE.allow_path[{index}] must not be empty")
                candidate = Path(value).expanduser()
                if not candidate.is_absolute():
                    raise ValueError(
                        f"WORKSPACE.allow_path entries must be absolute paths; relative path not allowed: {value!r}"
                    )
                normalized.append(_canonical(candidate))
            allow_paths = _dedupe(normalized)

        skill_roots = _dedupe([_canonical(path) for path in skills.roots]) if skills is not None else ()
        return cls(
            workspace_root=_canonical(workspace_root),
            allow_read_roots=allow_paths,
            skill_roots=skill_roots,
        )

    def can_read(self, path: str | Path) -> bool:
        return self._within_any(path, self.read_roots)

    def can_write(self, path: str | Path) -> bool:
        return self._within_any(path, self.write_roots)

    def require_read(self, path: str | Path) -> Path:
        resolved = _canonical(path)
        if not self.can_read(resolved):
            raise PermissionError(f"Read access denied outside configured roots: {resolved}")
        return resolved

    def require_write(self, path: str | Path) -> Path:
        resolved = _canonical(path)
        if not self.can_write(resolved):
            raise PermissionError(f"Write access denied outside workspace: {resolved}")
        return resolved

    @staticmethod
    def _within_any(path: str | Path, roots: tuple[Path, ...]) -> bool:
        resolved = _canonical(path)
        for root in roots:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False


@dataclass(frozen=True)
class SysOperationBinding:
    """Write-capable and controlled-read Jiuwen operations."""

    primary: Any
    read_only: Any
    primary_id: str
    read_only_id: str


def build_sys_operations(
    policy: WorkspaceAccessPolicy,
    *,
    agent_name: str,
    shell_allowlist: tuple[str, ...] | None = None,
) -> SysOperationBinding:
    """Register isolated Jiuwen operations for write and controlled-read tools."""
    from openjiuwen.core.runner.runner import Runner
    from openjiuwen.core.sys_operation import (
        LocalWorkConfig,
        OperationMode,
        SysOperation,
        SysOperationCard,
    )

    fingerprint_parts = [
        *(str(path) for path in (*policy.write_roots, *policy.read_roots)),
        "<bash-unrestricted>" if shell_allowlist is None else "<bash-restricted>",
        *(shell_allowlist or ()),
    ]
    fingerprint = hashlib.sha256("\0".join(fingerprint_parts).encode()).hexdigest()[:12]
    primary_id = f"{agent_name}_sysop_{fingerprint}"
    read_only_id = f"{agent_name}_read_sysop_{fingerprint}"

    cards = (
        SysOperationCard(
            id=primary_id,
            mode=OperationMode.LOCAL,
            work_config=LocalWorkConfig(
                shell_allowlist=(
                    None
                    if shell_allowlist is None
                    else list(shell_allowlist) or _DISABLED_SHELL_ALLOWLIST
                ),
                sandbox_root=[str(path) for path in policy.write_roots],
                restrict_to_sandbox=True,
            ),
        ),
        SysOperationCard(
            id=read_only_id,
            mode=OperationMode.LOCAL,
            work_config=LocalWorkConfig(
                shell_allowlist=[],
                sandbox_root=[str(path) for path in policy.read_roots],
                restrict_to_sandbox=True,
            ),
        ),
    )
    primary_card, read_only_card = cards
    if Runner.resource_mgr.get_sys_operation(primary_card.id) is None:
        result = Runner.resource_mgr.add_sys_operation(primary_card)
        if result.is_err():
            raise RuntimeError(f"Failed to register SysOperation {primary_card.id!r}: {result.msg()}")
    primary = Runner.resource_mgr.get_sys_operation(primary_id)
    if primary is None:
        raise RuntimeError("Failed to create primary workspace SysOperation")

    # Keep the broader read roots private. Registering this card globally would
    # also publish its internal write/code/shell operation resources.
    read_only = SysOperation(read_only_card)
    return SysOperationBinding(
        primary=primary,
        read_only=read_only,
        primary_id=primary_id,
        read_only_id=read_only_id,
    )
