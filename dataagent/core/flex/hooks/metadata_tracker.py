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
"""Flex-specific metadata tracker hooks（executor 节点 pre/post-hook）。

产物路径（基于 ~/.dataagent 体系）：

- 文件变更记录（跨 session，用户级）：
  ``~/.dataagent/{user_id}/.memory/file_metadata.json``

user_id 直接从 state["user_id"] 读取，不依赖工作区路径推断。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.workflow.state import FlexState
from dataagent.utils.constants import (
    DEFAULT_METADATA_DESCRIPTION_MAX_BYTES,
    DEFAULT_METADATA_TOOL_ARGS_MAX_BYTES,
    DEFAULT_METADATA_TRUNCATION_SUFFIX,
)
from dataagent.utils.runtime_paths import resolve_user_root

METADATA_FILE = "file_metadata.json"


# ── 路径 ─────────────────────────────────────────────────────────────────────


def _user_memory_dir(user_id: str) -> Path:
    path = resolve_user_root(user_id=user_id) / ".memory"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _metadata_path(user_id: str) -> Path:
    return _user_memory_dir(user_id) / METADATA_FILE


# ── JSON I/O ─────────────────────────────────────────────────────────────────


def _load_metadata(path: Path) -> dict[str, Any]:
    default: dict[str, Any] = {"version": 1, "updated_at": "", "files": {}}
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default
    if not isinstance(payload, dict):
        return default
    payload.setdefault("version", 1)
    payload.setdefault("updated_at", "")
    if not isinstance(payload.get("files"), dict):
        payload["files"] = {}
    return payload


def _save_metadata(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── 内部辅助 ──────────────────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
    abs_path = workspace_dir / rel_path
    if not abs_path.exists() or not abs_path.is_file():
        return

    stat_result = abs_path.stat()
    selected_call = _select_call(rel_path, calls)
    tool_name = str((selected_call or {}).get("name") or "unknown")
    raw_args = (selected_call or {}).get("args") or {}
    tool_args = truncate_text(
        json_dumps_compact(raw_args), DEFAULT_METADATA_TOOL_ARGS_MAX_BYTES, suffix=DEFAULT_METADATA_TRUNCATION_SUFFIX
    )
    purpose = ""
    if isinstance(raw_args, dict):
        purpose = str(raw_args.get("purpose", "") or "").strip()
        purpose = (
            truncate_text(purpose, DEFAULT_METADATA_DESCRIPTION_MAX_BYTES, suffix=DEFAULT_METADATA_TRUNCATION_SUFFIX)
            if purpose
            else ""
        )

    base_desc = purpose or description or f"{'Created' if status == 'created' else 'Modified'} by {tool_name}"
    base_desc = truncate_text(
        base_desc, DEFAULT_METADATA_DESCRIPTION_MAX_BYTES, suffix=DEFAULT_METADATA_TRUNCATION_SUFFIX
    )
    event_desc = truncate_text(
        f"{status}: {base_desc}", DEFAULT_METADATA_DESCRIPTION_MAX_BYTES, suffix=DEFAULT_METADATA_TRUNCATION_SUFFIX
    )

    existing = files.get(rel_path)
    bloodline: list[dict[str, Any]] = []
    if isinstance(existing, dict):
        old = existing.get("bloodline", [])
        if isinstance(old, list):
            bloodline = [e for e in old if isinstance(e, dict)]

    bloodline.append(
        {
            "tool_name": tool_name,
            "tool_args": tool_args,
            "description": event_desc,
            "changed_at": changed_at,
        }
    )

    files[rel_path] = {
        "name": abs_path.name,
        "description": base_desc,
        "size": int(stat_result.st_size),
        "num_lines": count_lines(abs_path),
        "relative_path": rel_path,
        "last_modified_at": changed_at,
        "bloodline": bloodline,
    }


def _select_call(rel_path: str, calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not calls:
        return None
    matched = [c for c in calls if rel_path in set(c.get("paths", []))]
    if len(matched) == 1:
        return matched[0]
    if len(calls) == 1:
        return calls[0]
    return matched[0] if matched else None


# ── 公开 hooks ────────────────────────────────────────────────────────────────


def pre_metadata_tracker(state: FlexState, runtime: Runtime) -> FlexState:
    """Executor 节点 pre-hook：记录执行前工作区快照与本轮 tool_calls。"""
    workspace_dir = Path(runtime.workspace_dir or Path.cwd()).resolve()
    fm = runtime.file_metadata
    fm.workspace_dir = workspace_dir
    fm.before_snapshot = workspace_snapshot(workspace_dir)

    latest = (list(state.get("messages") or []) or [None])[-1]
    raw_desc = content_to_text(getattr(latest, "content", "") if latest else "")
    description = (
        truncate_text(
            raw_desc.strip(), DEFAULT_METADATA_DESCRIPTION_MAX_BYTES, suffix=DEFAULT_METADATA_TRUNCATION_SUFFIX
        )
        if raw_desc
        else ""
    )
    tool_calls = getattr(latest, "tool_calls", []) if latest else []
    fm.turn_context = {
        "description": description,
        "tool_calls": [
            {
                "name": str((c or {}).get("name") or ""),
                "args": to_jsonable((c or {}).get("args") or {}),
                "paths": sorted(extract_paths_from_args((c or {}).get("args") or {}, workspace_dir)),
            }
            for c in tool_calls
        ],
    }
    # 将 user_id 存入 file_metadata 供 post hook 使用
    fm.user_id = str(state.get("user_id") or "")
    return state


def post_metadata_tracker(state: FlexState, runtime: Runtime) -> FlexState:
    """Executor 节点 post-hook：对比工作区 diff，更新 file_metadata.json。

    产物：``~/.dataagent/{user_id}/.memory/file_metadata.json``
    """
    fm = runtime.file_metadata
    workspace_dir = Path(fm.workspace_dir or runtime.workspace_dir or Path.cwd()).resolve()
    before_snapshot = fm.before_snapshot
    if not isinstance(before_snapshot, dict):
        return state

    after_snapshot = workspace_snapshot(workspace_dir)
    created, modified = snapshot_delta(before_snapshot, after_snapshot)
    if not created and not modified:
        return state

    user_id = getattr(fm, "user_id", None) or str(state.get("user_id") or "")
    if not user_id:
        return state

    meta_path = _metadata_path(user_id)
    payload = _load_metadata(meta_path)
    files = payload.setdefault("files", {})

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
    _save_metadata(meta_path, payload)
    return state
