"""Versioned DataTaskIR snapshots. Runtime caches are never the source of truth."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class SnapshotConflict(RuntimeError):
    """Another turn committed after this turn read its base revision."""


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".ir-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class IRSessionStore:
    """One store per user/session, even when several sessions share a workspace.

    The SDK also locks the workspace for a whole turn. This short file lock and
    revision check protect direct hook callers and reject lost updates. Version
    files are immutable; current.json is the only mutable pointer/snapshot.
    """

    def __init__(self, memory_dir: Path, user_id: str, session_id: str):
        self.owner = {"user_id": user_id, "session_id": session_id}
        self.root = memory_dir / "data_task_ir" / digest(self.owner)[:24]

    def load(self) -> dict | None:
        path = self.root / "current.json"
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("owner") != self.owner
                or value.get("status") not in {"draft", "committed", "awaiting_relation"}
                or not isinstance(value.get("revision"), str)
                or not isinstance(value.get("task_id"), str)
                or not isinstance(value.get("queries"), list)
                or not all(isinstance(q, str) for q in value["queries"])
                or not isinstance(value.get("fields"), list)
                or not all(isinstance(f, dict) and isinstance(f.get("field_id"), str)
                           and isinstance(f.get("value"), dict) for f in value["fields"])
                or not isinstance(value.get("evidence", []), list)
                or not all(isinstance(e, str) for e in value.get("evidence", []))):
            raise ValueError("DataTaskIR 会话快照损坏或版本不支持；请检查快照，或使用新的 session。")
        return value

    @contextmanager
    def _lock(self):
        # Match the Linux runtime; flock is released automatically on crash.
        import fcntl

        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "LOCK").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def save(self, value: dict, *, expected_revision: str | None) -> dict:
        with self._lock():
            previous = self.load()
            if (previous or {}).get("revision") != expected_revision:
                raise SnapshotConflict("DataTaskIR 已被另一轮更新，请重新开始本轮；不能覆盖新的会话口径。")
            snapshot = {
                **value, "schema_version": 1, "owner": self.owner,
                "revision": uuid.uuid4().hex, "parent_revision": expected_revision,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            atomic_json(self.root / "versions" / (snapshot["revision"] + ".json"), snapshot)
            atomic_json(self.root / "current.json", snapshot)
            return snapshot
