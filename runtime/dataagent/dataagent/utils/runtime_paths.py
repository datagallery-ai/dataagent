# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Runtime path helpers shared by agent, tools, and context modules.

Function groups and when to use them:

1. Runtime hierarchy
   - ``dataagent_home()``, ``resolve_user_root()``, ``resolve_session_root()``
   - Use when deriving DataAgent-managed writable directories.
   - These functions define the fixed path hierarchy:
     ``dataagent_home -> user_root -> session_root``.

2. Active workspace resolution
   - ``resolve_effective_workspace_root()``, ``resolve_session_framework_workspace()``
   - Use during runtime state initialization to decide the final workspace for
     the current run.
   - Priority is:
     code-level workspace override -> YAML ``WORKSPACE.path`` -> ``session_root``.
   - Optional YAML ``WORKSPACE.allow_path`` (list of absolute dirs) is loaded by the
     tool manager as extra **read-only** roots; same policy as skill package roots.

3. Workspace layout segments
   - ``resolve_workspace_layout()``, ``resolve_layout_dir()``, ``resolve_worker_root()``
   - Use for session framework directories (``.memory``, ``.context``, ``workers``, etc.)
     under the effective workspace root.

4. Package resources
   - ``dataagent_package_root()``, ``dataagent_package_path()``
   - Use when reading built-in package assets such as shipped YAML files,
     builtin skills, or templates.

5. Generic path normalization
   - ``resolve_runtime_path()``
   - Use only for ordinary user-supplied path arguments.
   - This helper does not decide workspace priority or DataAgent runtime hierarchy.

6. Runtime identity fallback
   - ``resolve_runtime_user_id()``
   - Use when hierarchy helpers need a stable user id but the caller did not
     provide one explicitly.

Design notes:
- This module owns path classification and workspace priority.
- ``workspace_guard`` should only audit already-resolved paths.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from dataagent.utils.constants import DEFAULT_WORKSPACE_LAYOUT
from dataagent.utils.env_utils import get_env

FLEX_PERSISTENCE_ROOT_ENV = "DATAAGENT_FLEX_PERSISTENCE_ROOT"
SUBAGENT_OUTPUT_DIR_ENV = "DATAAGENT_SUBAGENT_OUTPUT_DIR"

LayoutSegment = Literal[
    "session_memory_dir",
    "context_dir",
    "performance_dir",
    "workers_dir",
    "subagents_dir",
    "subagent_output_dir",
    "jobs_dir",
    "runtime_dump_dir",
    "tool_outputs_dir",
]

_LAYOUT_SEGMENT_NAMES: frozenset[str] = frozenset(DEFAULT_WORKSPACE_LAYOUT.keys())


@dataclass(frozen=True, slots=True)
class WorkspaceLayout:
    """Resolved workspace directory segment names."""

    session_memory_dir: str
    context_dir: str
    performance_dir: str
    workers_dir: str
    subagents_dir: str
    subagent_output_dir: str
    jobs_dir: str
    runtime_dump_dir: str
    tool_outputs_dir: str


def dataagent_home() -> Path:
    """Return the writable runtime home for DataAgent artifacts."""
    custom_home = get_env("DATAAGENT_HOME")
    if custom_home:
        return Path(custom_home).expanduser().resolve()
    return (Path.home() / ".dataagent").resolve()


def resolve_runtime_user_id(config: Mapping[str, Any] | None) -> str:
    """Return the normalized runtime user id."""
    if isinstance(config, Mapping):
        configured_user = config.get("USER_ID")
        if configured_user is not None and str(configured_user).strip():
            return str(configured_user).strip()
    return "anonymous"


def validate_user_id(user_id: str) -> str:
    """Normalize a user id and reject path traversal characters."""
    resolved_user_id = str(user_id).strip()
    if any(part in resolved_user_id for part in ("..", "/", "\\")):
        raise ValueError("user_id must not contain '..', '/' or '\\'.")
    return resolved_user_id


def resolve_user_root(*, user_id: str | None = None, config: Mapping[str, Any] | None = None) -> Path:
    """Return the fixed per-user root under DataAgent home."""
    resolved_user_id = (
        str(user_id).strip() if user_id is not None and str(user_id).strip() else resolve_runtime_user_id(config)
    )
    resolved_user_id = validate_user_id(resolved_user_id)
    return (dataagent_home() / resolved_user_id).resolve()


def resolve_session_root(
    *,
    session_id: str | None,
    user_id: str | None = None,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Return the fixed per-session root under DataAgent home."""
    session_value = str(session_id or "default_session")
    return (resolve_user_root(user_id=user_id, config=config) / session_value).resolve()


def is_job_subagent_workspace(path: str | Path, *, config: Mapping[str, Any] | None = None) -> bool:
    """Return True when ``path`` is ``{parent_ws}/<subagents_dir>/{session_id}/``."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.name:
        return False
    layout = resolve_workspace_layout(config)
    segment = Path(layout.subagents_dir)
    parent = resolved.parent
    seg_parts = segment.parts
    if len(parent.parts) < len(seg_parts):
        return False
    return parent.parts[-len(seg_parts) :] == seg_parts


def resolve_job_subagents_root(
    *,
    parent_workspace: str | Path,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Return ``{parent_ws}/<subagents_dir>`` for Job-path subagent workspaces."""
    parent_root = Path(parent_workspace).expanduser().resolve()
    layout = resolve_workspace_layout(config)
    return (parent_root / layout.subagents_dir).resolve()


def resolve_subagent_output_root(
    *,
    parent_workspace: str | Path,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Return the shared, read-only subagent output root under a parent workspace."""
    parent_root = Path(parent_workspace).expanduser().resolve()
    layout = resolve_workspace_layout(config)
    return (parent_root / layout.subagent_output_dir).resolve()


def is_subagent_output_sharing_enabled(config: Mapping[str, Any] | None) -> bool:
    """Return whether this parent Agent enables Job subagent output sharing.

    The feature is deliberately opt-in so existing Agent YAML files preserve
    their current Job and workspace behavior.
    """
    if not isinstance(config, Mapping):
        return False
    agent_config = config.get("AGENT_CONFIG")
    if not isinstance(agent_config, Mapping):
        return False
    return agent_config.get("subagent_output_sharing") is True


def resolve_jobs_root(
    *,
    parent_workspace: str | Path,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Return ``{parent_ws}/<jobs_dir>`` for job control-plane metadata."""
    parent_root = Path(parent_workspace).expanduser().resolve()
    layout = resolve_workspace_layout(config)
    return (parent_root / layout.jobs_dir).resolve()


def resolve_flex_storage_root(
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Return the root for Flex session artifacts (``.memory``, ``.context``).

    Job-path subagent subprocesses set ``DATAAGENT_FLEX_PERSISTENCE_ROOT`` to
    ``{parent_ws}/<subagents_dir>/{id}/``. When unset, an explicit ``workspace`` under
    the configured ``subagents_dir`` is used; otherwise fall back to
    ``resolve_session_framework_workspace``.
    """
    env_root = get_env(FLEX_PERSISTENCE_ROOT_ENV)
    if env_root and str(env_root).strip():
        return Path(env_root).expanduser().resolve()
    if workspace is not None and str(workspace).strip():
        candidate = Path(str(workspace)).expanduser().resolve()
        if is_job_subagent_workspace(candidate, config=config):
            return candidate
    return resolve_session_framework_workspace(
        workspace=workspace,
        config=config,
        session_id=session_id,
        user_id=user_id,
    )


def _is_job_flex_root(root: Path, *, config: Mapping[str, Any] | None = None) -> bool:
    """Return True when ``root`` is a job-path subagent persistence directory."""
    return is_job_subagent_workspace(root, config=config) or bool(get_env(FLEX_PERSISTENCE_ROOT_ENV))


def resolve_flex_session_memory_dir(
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Resolve session memory directory (layout segment or job-path ``.memory``)."""
    root = resolve_flex_storage_root(
        user_id=user_id,
        session_id=session_id,
        workspace=workspace,
        config=config,
    )
    if _is_job_flex_root(root, config=config):
        return root / ".memory"
    return resolve_layout_dir(root, "session_memory_dir", config=config)


def resolve_flex_context_dir(
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Resolve context directory (layout segment or job-path ``.context``)."""
    root = resolve_flex_storage_root(
        user_id=user_id,
        session_id=session_id,
        workspace=workspace,
        config=config,
    )
    if _is_job_flex_root(root, config=config):
        return root / ".context"
    return resolve_layout_dir(root, "context_dir", config=config)


def resolve_flex_performance_dir(
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Resolve performance directory (layout segment or job-path ``.performance``)."""
    root = resolve_flex_storage_root(
        user_id=user_id,
        session_id=session_id,
        workspace=workspace,
        config=config,
    )
    if _is_job_flex_root(root, config=config):
        return root / ".performance"
    return resolve_layout_dir(root, "performance_dir", config=config)


def resolve_workspace_layout(config: Mapping[str, Any] | None) -> WorkspaceLayout:
    """Merge ``WORKSPACE_POLICY.layout`` from config with package defaults."""
    merged = dict(DEFAULT_WORKSPACE_LAYOUT)
    if isinstance(config, Mapping):
        policy = config.get("WORKSPACE_POLICY")
        if isinstance(policy, Mapping):
            layout = policy.get("layout")
            if isinstance(layout, Mapping):
                for key, value in layout.items():
                    if key in merged and value is not None:
                        merged[key] = str(value)
    return WorkspaceLayout(**merged)


def resolve_layout_dir(
    workspace: Path | str,
    segment: LayoutSegment,
    *,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Resolve ``{workspace}/{layout[segment]}`` for a known layout segment."""
    if segment not in _LAYOUT_SEGMENT_NAMES:
        raise ValueError(f"Unknown workspace layout segment: {segment!r}")
    layout = resolve_workspace_layout(config)
    rel = getattr(layout, segment)
    return (Path(workspace) / rel).resolve()


def resolve_session_framework_workspace(
    *,
    workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
    session_id: str | None,
    user_id: str | None = None,
) -> Path:
    """Return the session framework root (explicit workspace or effective workspace)."""
    if workspace is not None:
        return Path(workspace).expanduser().resolve()
    return resolve_effective_workspace_root(config=config, session_id=session_id, user_id=user_id)


def resolve_worker_root(
    *,
    user_id: str | None,
    parent_session_id: str | None,
    sub_id: int,
    parent_workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Return the worker asset root under the parent session workspace."""
    parent_root = resolve_session_framework_workspace(
        workspace=parent_workspace,
        config=config,
        session_id=parent_session_id,
        user_id=user_id,
    )
    layout = resolve_workspace_layout(config)
    return (parent_root / layout.workers_dir / str(int(sub_id))).resolve()


def resolve_worker_memory_dir(
    *,
    user_id: str | None,
    parent_session_id: str | None,
    sub_id: int,
    parent_workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Return and create the worker ``.memory`` directory."""
    mem_dir = (
        resolve_worker_root(
            user_id=user_id,
            parent_session_id=parent_session_id,
            sub_id=sub_id,
            parent_workspace=parent_workspace,
            config=config,
        )
        / ".memory"
    )
    mem_dir.mkdir(parents=True, exist_ok=True)
    return mem_dir


def dataagent_package_root() -> Path:
    """Return the installed DataAgent package directory."""
    return Path(str(resources.files("dataagent"))).resolve()


def dataagent_package_path(*parts: str) -> Path:
    """Return a path inside the DataAgent package."""
    path = dataagent_package_root()
    for part in parts:
        path = path / part
    return path


def resolve_runtime_path(path: str | Path) -> Path:
    """Resolve a user-supplied runtime path.

    Relative paths are interpreted against the current working directory.
    Package-internal resources must use ``dataagent_package_path(...)`` explicitly.
    """

    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (Path.cwd() / candidate).resolve()


def resolve_effective_workspace_root(
    *,
    config: Mapping[str, Any] | None,
    session_id: str | None,
    user_id: str | None = None,
    workspace_override: str | Path | None = None,
) -> Path:
    """Resolve the effective workspace root.

    Priority:
    1. Code-level workspace override
    2. YAML ``WORKSPACE.path`` (non-empty values must be absolute; validated at config load)
    3. Default ``~/.dataagent/{user}/{session}``
    """

    if workspace_override is not None:
        override = Path(workspace_override).expanduser()
        if not override.is_absolute():
            override = override.resolve(strict=False)
        return override.resolve()

    if isinstance(config, Mapping):
        workspace = config.get("WORKSPACE")
        if isinstance(workspace, Mapping):
            configured_workspace = workspace.get("path")
            if configured_workspace:
                raw = str(configured_workspace).strip()
                if raw:
                    return resolve_runtime_path(raw)

    return resolve_session_root(session_id=session_id, user_id=user_id, config=config)
