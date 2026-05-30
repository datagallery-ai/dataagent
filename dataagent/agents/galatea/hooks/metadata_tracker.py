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

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dataagent.agents.galatea.state.state import State
from dataagent.agents.galatea.utils.json_store import read_json_object, write_json_object
from dataagent.agents.galatea.utils.metadata_utils import (
    content_to_text,
    count_lines,
    extract_paths_from_args,
    json_dumps_compact,
    snapshot_delta,
    to_jsonable,
    truncate_text,
    workspace_snapshot,
)
from dataagent.agents.galatea.utils.workspace_utils import user_id_from_workspace_dir, user_state_dir
from dataagent.core.cbb.runtime import Runtime

TOOL_ARGS_MAX_BYTES = 1024
DESCRIPTION_MAX_BYTES = 256
TRUNCATION_SUFFIX = " ...(truncated)"
METADATA_FILE_NAME = "file_metadata.json"


def pre_metadata_tracker(state: State, runtime: Runtime) -> State:
    """Pre metadata tracker."""
    workspace_dir = Path(getattr(runtime.env, "workspace_dir", Path.cwd())).resolve()
    fm = runtime.file_metadata
    fm.workspace_dir = workspace_dir
    fm.before_snapshot = workspace_snapshot(workspace_dir)

    latest_message = state["messages"][-1] if state.get("messages") else None
    raw_description = content_to_text(getattr(latest_message, "content", "") if latest_message else "")
    description = (
        truncate_text(raw_description.strip(), DESCRIPTION_MAX_BYTES, suffix=TRUNCATION_SUFFIX)
        if raw_description
        else ""
    )
    tool_calls = getattr(latest_message, "tool_calls", []) if latest_message else []
    fm.turn_context = {
        "description": description,
        "tool_calls": [
            {
                "name": str((call or {}).get("name") or ""),
                "args": to_jsonable((call or {}).get("args") or {}),
                "paths": sorted(extract_paths_from_args((call or {}).get("args") or {}, workspace_dir)),
            }
            for call in tool_calls
        ],
    }
    return state


def post_metadata_tracker(state: State, runtime: Runtime) -> State:
    """Post metadata tracker."""
    fm = runtime.file_metadata
    workspace_dir = fm.workspace_dir or Path(getattr(runtime.env, "workspace_dir", Path.cwd()))
    workspace_dir = Path(workspace_dir).resolve()
    before_snapshot = fm.before_snapshot
    if not isinstance(before_snapshot, dict):
        return state

    after_snapshot = workspace_snapshot(workspace_dir)
    created, modified = snapshot_delta(before_snapshot, after_snapshot)
    if not created and not modified:
        return state

    user_metadata_path = _user_metadata_path_from_workspace(workspace_dir)
    payload = _load_metadata(user_metadata_path)
    files = payload.setdefault("files", {})
    if not isinstance(files, dict):
        files = {}
        payload["files"] = files

    ctx = fm.turn_context
    calls = ctx.get("tool_calls", []) if isinstance(ctx, dict) else []
    description = str(ctx.get("description", "") if isinstance(ctx, dict) else "").strip()
    now = _utc_now_iso()

    for rel_path in created:
        _upsert_file_record(
            files=files,
            workspace_dir=workspace_dir,
            rel_path=rel_path,
            status="created",
            calls=calls,
            description=description,
            changed_at=now,
        )
    for rel_path in modified:
        _upsert_file_record(
            files=files,
            workspace_dir=workspace_dir,
            rel_path=rel_path,
            status="modified",
            calls=calls,
            description=description,
            changed_at=now,
        )

    payload["updated_at"] = now
    _save_metadata(user_metadata_path, payload)
    return state


def _upsert_file_record(
    *,
    files: dict[str, Any],
    workspace_dir: Path,
    rel_path: str,
    status: str,
    calls: list[dict[str, Any]],
    description: str,
    changed_at: str,
) -> None:
    """Upsert file record."""
    abs_path = workspace_dir / rel_path
    if not abs_path.exists() or not abs_path.is_file():
        return

    stat_result = abs_path.stat()
    selected_call = _select_call_for_path(rel_path, calls)
    tool_name = str((selected_call or {}).get("name") or "unknown")
    raw_tool_args = (selected_call or {}).get("args") or {}
    tool_args_json = json_dumps_compact(raw_tool_args)
    tool_args = truncate_text(tool_args_json, TOOL_ARGS_MAX_BYTES, suffix=TRUNCATION_SUFFIX)
    purpose = ""
    if isinstance(raw_tool_args, dict):
        purpose = str(raw_tool_args.get("purpose", "") or "").strip()
        purpose = truncate_text(purpose, DESCRIPTION_MAX_BYTES, suffix=TRUNCATION_SUFFIX) if purpose else ""

    base_description = purpose or description or f"{'Created' if status == 'created' else 'Modified'} by {tool_name}"
    base_description = truncate_text(base_description, DESCRIPTION_MAX_BYTES, suffix=TRUNCATION_SUFFIX)
    event_description = truncate_text(f"{status}: {base_description}", DESCRIPTION_MAX_BYTES, suffix=TRUNCATION_SUFFIX)

    existing = files.get(rel_path)
    bloodline: list[dict[str, Any]] = []
    if isinstance(existing, dict):
        old_bloodline = existing.get("bloodline", [])
        if isinstance(old_bloodline, list):
            bloodline = [entry for entry in old_bloodline if isinstance(entry, dict)]

    bloodline.append(
        {"tool_name": tool_name, "tool_args": tool_args, "description": event_description, "changed_at": changed_at}
    )

    files[rel_path] = {
        "name": abs_path.name,
        "description": base_description,
        "size": int(stat_result.st_size),
        "num_lines": count_lines(abs_path),
        "relative_path": rel_path,
        "last_modified_at": changed_at,
        "bloodline": bloodline,
    }


def _select_call_for_path(rel_path: str, calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Select call for path."""
    if not isinstance(calls, list) or not calls:
        return None
    matched = [call for call in calls if rel_path in set(call.get("paths", []))]
    if len(matched) == 1:
        return matched[0]
    if len(calls) == 1:
        return calls[0]
    return matched[0] if matched else None


def _user_metadata_path_from_workspace(workspace_dir: Path) -> Path:
    """User metadata path from workspace."""
    user_id = user_id_from_workspace_dir(workspace_dir)
    return user_state_dir(user_id) / METADATA_FILE_NAME


def _load_metadata(path: Path) -> dict[str, Any]:
    """Load metadata."""
    default: dict[str, Any] = {"version": 1, "updated_at": "", "files": {}}
    payload = read_json_object(path, default)
    files = payload.get("files", {})
    if not isinstance(files, dict):
        payload["files"] = {}
    payload.setdefault("version", 1)
    payload.setdefault("updated_at", "")
    return payload


def _save_metadata(path: Path, payload: dict[str, Any]) -> None:
    """Save metadata."""
    write_json_object(path, payload)


def _utc_now_iso() -> str:
    """UTC now ISO."""
    return datetime.now(UTC).isoformat()
