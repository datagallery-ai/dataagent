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
   - ``resolve_effective_workspace_root()``
   - Use during runtime state initialization to decide the final workspace for
     the current run.
   - Priority is:
     code-level workspace override -> YAML ``WORKSPACE.path`` -> ``session_root``.
   - Optional YAML ``WORKSPACE.allow_path`` (list of absolute dirs) is loaded by the
     tool manager as extra **read-only** roots; same policy as skill package roots.

3. Package resources
   - ``dataagent_package_root()``, ``dataagent_package_path()``
   - Use when reading built-in package assets such as shipped YAML files,
     builtin skills, or templates.

4. Generic path normalization
   - ``resolve_runtime_path()``
   - Use only for ordinary user-supplied path arguments.
   - This helper does not decide workspace priority or DataAgent runtime hierarchy.

5. Runtime identity fallback
   - ``resolve_runtime_user_id()``
   - Use when hierarchy helpers need a stable user id but the caller did not
     provide one explicitly.

Design notes:
- This module owns path classification and workspace priority.
- ``workspace_guard`` should only audit already-resolved paths.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

from dataagent.utils.env_utils import get_env


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


def resolve_user_root(*, user_id: str | None = None, config: Mapping[str, Any] | None = None) -> Path:
    """Return the fixed per-user root under DataAgent home."""
    resolved_user_id = (
        str(user_id).strip() if user_id is not None and str(user_id).strip() else resolve_runtime_user_id(config)
    )
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


def resolve_worker_root(*, user_id: str | None, parent_session_id: str | None, sub_id: int) -> Path:
    """Return the fixed worker asset root under a parent session."""
    session_root = resolve_session_root(user_id=user_id, session_id=parent_session_id)
    return (session_root / "workers" / str(int(sub_id))).resolve()


def resolve_worker_memory_dir(*, user_id: str | None, parent_session_id: str | None, sub_id: int) -> Path:
    """Return and create the worker ``.memory`` directory."""
    mem_dir = resolve_worker_root(user_id=user_id, parent_session_id=parent_session_id, sub_id=sub_id) / ".memory"
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
