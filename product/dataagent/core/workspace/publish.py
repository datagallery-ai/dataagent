"""Publish business artifacts from Job subagents into a shared read-only area."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dataagent.utils.runtime_paths import resolve_subagent_output_root

MANIFEST_FILENAME = "manifest.json"


def ensure_subagent_output_root(*, parent_workspace: str | Path, config: Mapping[str, Any] | None = None) -> Path:
    """Create and return the shared subagent output root."""
    root = resolve_subagent_output_root(parent_workspace=parent_workspace, config=config)
    root.mkdir(parents=True, exist_ok=True)
    return root


def load_publish_manifest(root: str | Path) -> dict[str, Any]:
    """Load the publish manifest; a missing or malformed file means no entries."""
    path = Path(root).expanduser().resolve() / MANIFEST_FILENAME
    if not path.is_file():
        return {"version": 1, "entries": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "entries": []}
    if not isinstance(payload, dict):
        return {"version": 1, "entries": []}
    entries = payload.get("entries")
    return {"version": 1, "entries": entries if isinstance(entries, list) else []}


def list_published_artifacts(published_dir: str | Path) -> list[str]:
    """List top-level business artifacts in a published subagent directory."""
    root = Path(published_dir).expanduser().resolve()
    if not root.is_dir():
        return []
    return [
        f"{child.name}/" if child.is_dir() else child.name
        for child in sorted(root.iterdir(), key=lambda item: item.name)
        if not child.name.startswith(".")
    ]


def publish_subagent_artifacts(
    *,
    source_workspace: str | Path,
    parent_workspace: str | Path,
    subagent_session_id: str,
    agent_id: str,
    task: str,
    job_id: str,
    config: Mapping[str, Any] | None = None,
) -> Path:
    """Copy business artifacts and upsert their discoverable manifest entry.

    Only top-level non-dot entries are copied. Symlinks are skipped rather than
    followed so a subagent cannot publish data outside its workspace tree.
    """
    session_id = _safe_session_id(subagent_session_id)
    source = Path(source_workspace).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"subagent workspace does not exist: {source}")
    root = ensure_subagent_output_root(parent_workspace=parent_workspace, config=config)
    target = root / session_id
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{session_id}.", dir=root))
    target_created = False
    try:
        _copy_business_artifacts(source, temp_dir)
        if target.exists():
            shutil.rmtree(target)
        os.replace(temp_dir, target)
        target_created = True
        _upsert_manifest(
            root=root,
            subagent_session_id=session_id,
            agent_id=agent_id,
            task=task,
            job_id=job_id,
            published_path=target,
        )
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        if target_created:
            shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def _copy_business_artifacts(source: Path, target: Path) -> None:
    for child in source.iterdir():
        if child.name.startswith(".") or child.is_symlink():
            continue
        destination = target / child.name
        if child.is_dir():
            _copy_directory(child, destination)
        elif child.is_file():
            shutil.copy2(child, destination)


def _copy_directory(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        if child.is_symlink():
            continue
        destination = target / child.name
        if child.is_dir():
            _copy_directory(child, destination)
        elif child.is_file():
            shutil.copy2(child, destination)


def _upsert_manifest(
    *,
    root: Path,
    subagent_session_id: str,
    agent_id: str,
    task: str,
    job_id: str,
    published_path: Path,
) -> None:
    manifest = load_publish_manifest(root)
    entries = [
        entry
        for entry in manifest["entries"]
        if not isinstance(entry, dict) or entry.get("subagent_id") != subagent_session_id
    ]
    now = datetime.now(UTC).isoformat()
    entries.append(
        {
            "subagent_id": subagent_session_id,
            "agent_id": str(agent_id or ""),
            "task": str(task or ""),
            "job_id": str(job_id or ""),
            "published_path": str(published_path),
            "artifacts": list_published_artifacts(published_path),
            "updated_at": now,
        }
    )
    payload = {"version": 1, "updated_at": now, "entries": entries}
    temp_path = root / f".{MANIFEST_FILENAME}.tmp"
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, root / MANIFEST_FILENAME)


def _safe_session_id(value: str) -> str:
    session_id = str(value or "").strip()
    if (
        not session_id
        or Path(session_id).name != session_id
        or "/" in session_id
        or "\\" in session_id
        or session_id in {".", ".."}
    ):
        raise ValueError("subagent_session_id must be one safe path segment")
    return session_id
