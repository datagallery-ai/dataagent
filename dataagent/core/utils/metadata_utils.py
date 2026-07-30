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


def snapshot_delta(
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
) -> tuple[list[str], list[str]]:
    """Snapshot delta."""
    created = sorted(set(after) - set(before))
    modified = sorted(path for path in set(after).intersection(before) if before[path] != after[path])
    return created, modified


def workspace_snapshot(workspace_dir: Path) -> dict[str, tuple[int, int]]:
    """Workspace snapshot."""
    result: dict[str, tuple[int, int]] = {}
    for path in workspace_dir.rglob("*"):
        if not path.is_file():
            continue
        rel_path = path.relative_to(workspace_dir).as_posix()
        rel_parts = Path(rel_path).parts
        if is_internal_file(rel_parts):
            continue
        try:
            stat_result = path.stat()
        except OSError:
            continue
        result[rel_path] = (int(stat_result.st_size), int(stat_result.st_mtime_ns))
    return result


def is_internal_file(parts: tuple[str, ...]) -> bool:
    """Is internal file."""
    if not parts:
        return True
    return parts[0] in {".galatea", ".git", "__pycache__"}


def extract_paths_from_args(args: Any, workspace_dir: Path) -> set[str]:
    """Extract paths from arguments."""
    found: set[str] = set()

    def _walk(value: Any, key_hint: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                _walk(child, str(key).lower())
            return
        if isinstance(value, list):
            for child in value:
                _walk(child, key_hint)
            return
        if isinstance(value, str) and key_hint in {"path", "relative_path", "file_path", "file"}:
            rel = normalize_rel_path(value, workspace_dir)
            if rel:
                found.add(rel)

    _walk(args)
    return found


def normalize_rel_path(path_value: str, workspace_dir: Path) -> str | None:
    """Normalize relative path."""
    text = str(path_value).strip()
    if not text:
        return None
    raw_path = Path(text)
    candidate = (workspace_dir / raw_path).resolve() if not raw_path.is_absolute() else raw_path.resolve()
    try:
        rel = candidate.relative_to(workspace_dir).as_posix()
    except ValueError:
        return None
    if not rel or rel.startswith("../"):
        return None
    return rel


def count_lines(path: Path) -> int:
    """Count lines."""
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    return len(content.splitlines())


def content_to_text(content: Any) -> str:
    """Convert content to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                chunks.append(text if isinstance(text, str) else str(item))
            else:
                chunks.append(str(item))
        return "\n".join(chunks).strip()
    return str(content)


def to_jsonable(obj: Any) -> Any:
    """Convert to JSONable."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(item) for item in obj]
    return f"<{type(obj).__name__}>"


def truncate_text(text: str, max_bytes: int, *, suffix: str = " ...(truncated)") -> str:
    """Truncate text."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    suffix_bytes = suffix.encode("utf-8")
    if max_bytes <= len(suffix_bytes):
        return suffix[: max(1, max_bytes)]
    clipped = raw[: max_bytes - len(suffix_bytes)].decode("utf-8", errors="ignore")
    return f"{clipped}{suffix}"


def json_dumps_compact(value: Any) -> str:
    """JSON dumps compact."""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)
