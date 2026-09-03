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
"""Cooperative filesystem locks for workspace roots.

This is a seat-taking protocol (atomic ``mkdir`` of ``{workspace}/.lock/``), not
an OS-level write barrier. Only call sites that ``acquire`` are mutually excluded.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

# Fixed TTL for main-agent session workspace locks (product decision: 2 hours).
MAIN_SESSION_LOCK_TTL_SECONDS = 2 * 60 * 60


@dataclass(frozen=True)
class WorkspaceLockHandle:
    """Token identifying an acquired workspace filesystem lock."""

    workspace_root: Path
    lock_dir: Path
    token: str
    owner_kind: str
    owner_id: str
    purpose: str
    expires_at: str


class WorkspaceBusyError(RuntimeError):
    """Raised when a workspace root is already locked by another live owner."""

    @classmethod
    def from_workspace(cls, workspace_root: Path | str) -> WorkspaceBusyError:
        """Build a busy error with owner metadata when ``lock.json`` is readable.

        Args:
            workspace_root: Absolute workspace directory that failed acquire.

        Returns:
            ``WorkspaceBusyError`` whose message includes owner fields when present.
            The message never includes the lock-file path, so callers (including LLMs)
            are not prompted to delete the lock.
        """
        root = Path(workspace_root).expanduser()
        meta = _read_lock_payload(root / ".lock" / "lock.json")
        if not meta:
            return cls(f"Session workspace is busy: {root}")
        owner_kind = meta.get("owner_kind") or "unknown"
        owner_id = meta.get("owner_id") or "unknown"
        purpose = meta.get("purpose") or "unknown"
        expires_at = meta.get("expires_at") or "unknown"
        return cls(
            "Session workspace is busy "
            f"(workspace={root}, owner_kind={owner_kind}, owner_id={owner_id}, "
            f"purpose={purpose}, expires_at={expires_at})"
        )


def acquire_workspace_lock(
    *,
    workspace_root: Path | str,
    owner_kind: str,
    owner_id: str,
    purpose: str,
    ttl_seconds: int,
) -> WorkspaceLockHandle | None:
    """Acquire an atomic directory lock for one workspace root.

    Args:
        workspace_root: Absolute directory that owns ``.lock/``.
        owner_kind: Logical owner class (e.g. ``main_session``).
        owner_id: Owner identity (e.g. session id).
        purpose: Short reason string (e.g. ``agent_chat``).
        ttl_seconds: Lock lifetime; expired locks are cleaned as stale.

    Returns:
        ``WorkspaceLockHandle`` on success, or ``None`` when another live lock exists.
    """
    root = Path(workspace_root).expanduser().resolve()
    lock_dir = root / ".lock"
    root.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_lock(lock_dir)
    token = str(uuid.uuid4())
    expires_at = datetime.now(UTC) + timedelta(seconds=max(1, int(ttl_seconds)))
    try:
        lock_dir.mkdir()
    except FileExistsError:
        return None
    payload = {
        "token": token,
        "owner_kind": str(owner_kind or ""),
        "owner_id": str(owner_id or ""),
        "purpose": str(purpose or ""),
        "owner_pid": os.getpid(),
        "hostname": socket.gethostname(),
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    (lock_dir / "lock.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return WorkspaceLockHandle(
        workspace_root=root,
        lock_dir=lock_dir,
        token=token,
        owner_kind=payload["owner_kind"],
        owner_id=payload["owner_id"],
        purpose=payload["purpose"],
        expires_at=payload["expires_at"],
    )


def release_workspace_lock(handle: WorkspaceLockHandle) -> None:
    """Release a workspace lock only if the on-disk token still matches ``handle``.

    Args:
        handle: Previously acquired lock handle.
    """
    lock_dir = Path(handle.lock_dir)
    lock_file = lock_dir / "lock.json"
    payload = _read_lock_payload(lock_file)
    if not payload or payload.get("token") != handle.token:
        return
    try:
        shutil.rmtree(lock_dir)
    except OSError as exc:
        logger.warning("Failed to release workspace lock {}: {}", lock_dir, exc)


def is_workspace_lock_present(workspace_root: Path | str) -> bool:
    """Return whether ``{workspace_root}/.lock`` currently exists.

    Args:
        workspace_root: Absolute workspace directory to probe.

    Returns:
        ``True`` when the lock directory is present (live or stale until cleaned).
    """
    return (Path(workspace_root).expanduser() / ".lock").exists()


def _read_lock_payload(lock_file: Path) -> dict[str, Any] | None:
    """Load ``lock.json`` as a dict, or ``None`` on missing/invalid content.

    Args:
        lock_file: Path to ``lock.json``.

    Returns:
        Parsed payload dict, or ``None``.
    """
    try:
        payload = json.loads(lock_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Failed to read workspace lock payload {}: {}", lock_file, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _cleanup_stale_lock(lock_dir: Path) -> None:
    """Remove an expired lock directory without deleting a fresh replacement.

    Args:
        lock_dir: Candidate ``.lock`` directory under a workspace root.
    """
    lock_file = lock_dir / "lock.json"
    if not lock_dir.exists():
        return
    payload = _read_lock_payload(lock_file)
    try:
        expires_at = datetime.fromisoformat(str((payload or {}).get("expires_at")))
    except (TypeError, ValueError) as exc:
        logger.warning("Invalid workspace lock expires_at in {}; treating as stale: {}", lock_file, exc)
        expires_at = datetime.min.replace(tzinfo=UTC)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at > datetime.now(UTC):
        return
    stale_dir = lock_dir.with_name(f".lock.stale.{uuid.uuid4().hex}")
    try:
        lock_dir.rename(stale_dir)
    except OSError as exc:
        logger.warning("Failed to rename stale workspace lock {}: {}", lock_dir, exc)
        return
    try:
        shutil.rmtree(stale_dir)
    except OSError as exc:
        logger.warning("Failed to remove stale workspace lock {}: {}", stale_dir, exc)
