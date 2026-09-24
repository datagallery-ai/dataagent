"""Caller-supplied launch inputs only; no configuration loading or prepared results."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LaunchOptions:
    """What a host or SDK caller knows before any file is read."""

    cwd: str | Path = field(default_factory=Path.cwd)
    workspace: str | Path | None = None
    workspaces: tuple[str | Path, ...] = ()
    user_id: str = "default"
    session_id: str | None = None
    config: str | Path | None = None
    env_file: str | Path | None = None
    init_home: bool = False
