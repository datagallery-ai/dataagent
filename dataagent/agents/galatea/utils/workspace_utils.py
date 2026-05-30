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
from __future__ import annotations

import re
from pathlib import Path


def safe_segment(value: str) -> str:
    """Safe segment."""
    text = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value).strip())
    return text[:80] or "default"


def project_root() -> Path:
    """Project root."""
    return Path(__file__).resolve().parent.parent.parent.parent


def workspaces_root() -> Path:
    """Workspaces root."""
    root = project_root() / ".workspaces"
    root.mkdir(parents=True, exist_ok=True)
    return root


def workspace_state_dir(workspace_dir: Path) -> Path:
    """Workspace state directory."""
    state_dir = workspace_dir / ".galatea"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def user_state_dir(user_id: str) -> Path:
    """User state directory."""
    state_dir = workspaces_root() / safe_segment(user_id) / ".galatea"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def workspace_dir_for_session(user_id: str, session_id: str) -> Path:
    """Workspace directory for session."""
    workspace = workspaces_root() / safe_segment(user_id) / safe_segment(session_id)
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def user_id_from_workspace_dir(workspace_dir: Path) -> str:
    """User ID from workspace directory."""
    try:
        relative = workspace_dir.resolve().relative_to(workspaces_root())
        parts = relative.parts
        if parts:
            return parts[0]
    except (ValueError, OSError):
        pass
    return "default"
