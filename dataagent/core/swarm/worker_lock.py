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

"""这个锁解决的是同一个 worker 目录并发写入问题。即便 executor 已经拦截同一轮重复 `sub_id`，仍然需要文件锁兜底，因为可能存在跨轮、跨进程或异常路径。"""

from __future__ import annotations

import json
import os
import shutil
import socket
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from dataagent.utils.runtime_paths import resolve_worker_root


@dataclass
class WorkerLock:
    """Token identifying an acquired worker filesystem lock."""

    sub_id: int
    lock_dir: Path
    token: str
    expires_at: str


def acquire_worker_lock(
    *,
    user_id: str,
    parent_session_id: str,
    sub_id: int,
    query: str,
    ttl_seconds: int,
    parent_workspace: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> WorkerLock | None:
    """Acquire an atomic directory lock for one ``parent_session_id + sub_id``.

    Returns ``None`` when another live invocation owns the lock. Expired locks are
    first moved aside and removed so a crashed parent process does not block
    future reuse forever.
    """
    lock_dir = (
        resolve_worker_root(
            user_id=user_id,
            parent_session_id=parent_session_id,
            sub_id=sub_id,
            parent_workspace=parent_workspace,
            config=config,
        )
        / ".lock"
    )
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_lock(lock_dir)
    token = str(uuid.uuid4())
    expires_at = datetime.now(UTC) + timedelta(seconds=max(1, int(ttl_seconds)))
    try:
        lock_dir.mkdir()
    except FileExistsError:
        return None
    payload = {
        "token": token,
        "owner_pid": os.getpid(),
        "hostname": socket.gethostname(),
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": expires_at.isoformat(),
        "query": query,
    }
    (lock_dir / "lock.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return WorkerLock(sub_id=int(sub_id), lock_dir=lock_dir, token=token, expires_at=payload["expires_at"])


def release_worker_lock(lock: WorkerLock) -> None:
    """Release a worker lock only if the on-disk token still matches ``lock``."""
    lock_dir = Path(lock.lock_dir)
    lock_file = lock_dir / "lock.json"
    try:
        payload = json.loads(lock_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if payload.get("token") != lock.token:
        return
    shutil.rmtree(lock_dir, ignore_errors=True)


def _cleanup_stale_lock(lock_dir: Path) -> None:
    """Remove an expired lock directory without deleting a fresh replacement."""
    lock_file = lock_dir / "lock.json"
    if not lock_dir.exists():
        return
    try:
        payload = json.loads(lock_file.read_text(encoding="utf-8"))
        expires_at = datetime.fromisoformat(str(payload.get("expires_at")))
    except (OSError, ValueError):
        expires_at = datetime.min.replace(tzinfo=UTC)
    if expires_at > datetime.now(UTC):
        return
    stale_dir = lock_dir.with_name(f".lock.stale.{uuid.uuid4().hex}")
    try:
        lock_dir.rename(stale_dir)
    except OSError:
        return
    shutil.rmtree(stale_dir, ignore_errors=True)
