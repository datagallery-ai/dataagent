# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Subagent workspace session allocation under ``{parent_ws}/<subagents_dir>/``."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dataagent.utils.runtime_paths import is_job_subagent_workspace, resolve_job_subagents_root

_SESSION_SEGMENT_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def normalize_subagent_session_id(raw: str) -> str:
    """Normalize a session id into a safe single path segment."""
    cleaned = _SESSION_SEGMENT_RE.sub("_", str(raw or "").strip()).strip("._-")
    return cleaned or uuid.uuid4().hex


def allocate_subagent_session_id() -> str:
    """Allocate a new opaque subagent session id."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class SubagentWorkspaceSession:
    """One subagent workspace binding for a single job."""

    subagent_session_id: str
    workspace_dir: Path
    workspace_rel_path: str


def prepare_subagent_workspace(
    *,
    parent_workspace: Path,
    session_id: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> SubagentWorkspaceSession:
    """Create ``{parent_ws}/<subagents_dir>/{id}/`` for one new job.

    Args:
        parent_workspace: Resolved main Agent workspace root.
        session_id: Optional explicit id; defaults to a newly allocated id.
        config: Merged agent config for ``WORKSPACE_POLICY.layout.subagents_dir``.

    Returns:
        Session metadata including absolute and relative workspace paths.
    """
    parent = Path(parent_workspace).expanduser().resolve()
    subagents_root = resolve_job_subagents_root(parent_workspace=parent, config=config)
    subagent_session_id = normalize_subagent_session_id(session_id or allocate_subagent_session_id())
    workspace_dir = (subagents_root / subagent_session_id).resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    workspace_rel_path = workspace_dir.relative_to(parent).as_posix()
    return SubagentWorkspaceSession(
        subagent_session_id=subagent_session_id,
        workspace_dir=workspace_dir,
        workspace_rel_path=workspace_rel_path,
    )


def resolve_subagent_workspace_session(
    *,
    parent_workspace: Path,
    workspace_rel_path: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> SubagentWorkspaceSession:
    """Allocate a new subagent workspace or bind an existing reused relative path.

    Args:
        parent_workspace: Resolved main Agent workspace root.
        workspace_rel_path: Optional relative path under ``parent_workspace`` (for example
            ``subagents/{id}``). When omitted, a fresh workspace directory is created.
        config: Merged agent config for ``WORKSPACE_POLICY.layout.subagents_dir``.

    Returns:
        Session metadata for the new or reused workspace.

    Raises:
        ValueError: When ``workspace_rel_path`` is invalid, outside ``parent_workspace``,
            not a job subagent directory, or missing on disk.
    """
    rel = str(workspace_rel_path or "").strip()
    if not rel:
        return prepare_subagent_workspace(parent_workspace=parent_workspace, config=config)

    parent = Path(parent_workspace).expanduser().resolve()
    rel_path = Path(rel.replace("\\", "/"))
    if rel_path.is_absolute():
        raise ValueError("workspace_rel_path must be a relative path under the parent workspace.")
    if ".." in rel_path.parts:
        raise ValueError("workspace_rel_path must not contain '..'.")

    workspace_dir = (parent / rel_path).resolve()
    try:
        workspace_dir.relative_to(parent)
    except ValueError as exc:
        raise ValueError("workspace_rel_path must resolve inside the parent workspace.") from exc

    if not is_job_subagent_workspace(workspace_dir, config=config):
        raise ValueError("workspace_rel_path must point to an existing job subagent workspace directory.")
    if not workspace_dir.is_dir():
        raise ValueError(f"workspace_rel_path does not exist: {rel}")

    subagent_session_id = normalize_subagent_session_id(workspace_dir.name)
    if subagent_session_id != workspace_dir.name:
        raise ValueError("workspace_rel_path must end with a valid subagent session id segment.")

    subagents_root = resolve_job_subagents_root(parent_workspace=parent, config=config)
    try:
        workspace_dir.relative_to(subagents_root.resolve())
    except ValueError as exc:
        raise ValueError("workspace_rel_path must stay under the configured subagents directory.") from exc

    return SubagentWorkspaceSession(
        subagent_session_id=subagent_session_id,
        workspace_dir=workspace_dir,
        workspace_rel_path=workspace_dir.relative_to(parent).as_posix(),
    )
