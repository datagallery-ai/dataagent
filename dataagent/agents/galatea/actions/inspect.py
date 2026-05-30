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

import json
from pathlib import Path
from typing import Any


def inspect(relative_path: str) -> dict[str, Any]:
    """
    Inspect stored file metadata from the workspace-level metadata store.

    Use this tool when:
    - You need metadata for a generated/modified file by relative path.
    - You want to inspect file lineage (tool name, tool args, change description).
    - You need size/line-count/last-modified context before further actions.

    Do not use this tool when:
    - You only need raw file content; use `read` instead.
    - You only have a filename without a relative path; resolve the path first.

    Args:
        relative_path: Unique file identifier relative to the workspace root.

    Returns:
        The metadata record for the given relative path.
        If not found or invalid, returns an error payload with `error`.
    """
    query_path = str(relative_path or "").strip().replace("\\", "/").lstrip("./")
    if not query_path:
        return {"error": "relative_path is required"}

    metadata_path = _resolve_user_metadata_path(Path.cwd().resolve())
    if metadata_path is None or not metadata_path.exists():
        return {"error": "metadata store not found"}

    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return {"error": "metadata store parse failed"}

    files = payload.get("files", {})
    if not isinstance(files, dict):
        return {"error": "invalid metadata schema"}

    record = files.get(query_path)
    if not isinstance(record, dict):
        return {"error": f"no metadata found for relative_path={query_path}"}

    return record


def _resolve_user_metadata_path(cwd: Path) -> Path | None:
    parts = cwd.parts
    if ".workspaces" not in parts:
        return None
    idx = parts.index(".workspaces")
    if idx + 2 >= len(parts):
        return None
    root = Path(*parts[: idx + 1])
    user_id = parts[idx + 1]
    return root / user_id / ".galatea" / "file_metadata.json"
