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
from pathlib import Path
from typing import Any

from dataagent.agents.galatea.utils.json_store import read_json_object, write_json_object
from dataagent.agents.galatea.utils.workspace_utils import user_state_dir, workspace_state_dir


def default_user_snapshot() -> dict[str, Any]:
    """Default user snapshot."""
    return {
        "goals": [],
        "constraints": [],
        "decisions": [],
        "important_findings": [],
        "artifacts": [],
    }


def default_user_profile() -> dict[str, Any]:
    """Default user profile."""
    return {
        "identity": "",
        "technical_level": "",
        "preferences": "",
        "recurring_topics": [],
    }


def _legacy_workspace_snapshot(workspace_dir: Path | None) -> dict[str, Any]:
    """Legacy workspace snapshot."""
    if workspace_dir is None:
        return default_user_snapshot()
    payload = read_json_object(
        workspace_state_dir(workspace_dir) / "memory.json",
        {"session_history": default_user_snapshot()},
    )
    session_history = payload.get("session_history", {})
    return session_history if isinstance(session_history, dict) else default_user_snapshot()


def load_user_snapshot(user_id: str, workspace_dir: Path | None = None) -> dict[str, Any]:
    """Load user snapshot."""
    if workspace_dir is not None:
        snapshot_path = workspace_state_dir(workspace_dir) / "snapshot.json"
        payload = read_json_object(snapshot_path, {})
        user_snapshot = payload.get("user_snapshot")
        if isinstance(user_snapshot, dict):
            return user_snapshot
        return _legacy_workspace_snapshot(workspace_dir)

    snapshot_path = user_state_dir(user_id) / "snapshot.json"
    payload = read_json_object(snapshot_path, {})
    user_snapshot = payload.get("user_snapshot")
    if isinstance(user_snapshot, dict):
        return user_snapshot
    return _legacy_workspace_snapshot(workspace_dir)


def save_user_snapshot(user_id: str, user_snapshot: dict[str, Any], workspace_dir: Path | None = None) -> None:
    """Save user snapshot."""
    snapshot_path = (
        workspace_state_dir(workspace_dir) / "snapshot.json"
        if workspace_dir is not None
        else user_state_dir(user_id) / "snapshot.json"
    )
    write_json_object(snapshot_path, {"user_snapshot": user_snapshot})


def load_user_profile(user_id: str, workspace_dir: Path | None = None) -> dict[str, Any]:
    """Load user profile."""
    profile_path = (
        workspace_state_dir(workspace_dir) / "profile.json"
        if workspace_dir is not None
        else user_state_dir(user_id) / "profile.json"
    )
    payload = read_json_object(profile_path, {"user_profile": default_user_profile()})
    user_profile = payload.get("user_profile", {})
    return user_profile if isinstance(user_profile, dict) else default_user_profile()


def save_user_profile(user_id: str, user_profile: dict[str, Any], workspace_dir: Path | None = None) -> None:
    """Save user profile."""
    profile_path = (
        workspace_state_dir(workspace_dir) / "profile.json"
        if workspace_dir is not None
        else user_state_dir(user_id) / "profile.json"
    )
    write_json_object(profile_path, {"user_profile": user_profile})


def save_user_messages_snapshot(
    user_id: str,
    messages: list[dict[str, Any]],
    workspace_dir: Path | None = None,
) -> None:
    """Save user messages snapshot."""
    snapshot_path = (
        workspace_state_dir(workspace_dir) / "messages_snapshot.json"
        if workspace_dir is not None
        else user_state_dir(user_id) / "messages_snapshot.json"
    )
    write_json_object(snapshot_path, {"messages": messages})
