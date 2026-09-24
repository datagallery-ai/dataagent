"""A small durable session index. Conversation state belongs to LangGraph."""

import asyncio
import fcntl
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite


@contextmanager
def workspace_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_dir / "backend.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another DataAgent V2 backend owns this workspace") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Sessions:
    def __init__(self, path: Path):
        self.path = path
        self._write_lock = asyncio.Lock()

    async def __aenter__(self):
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        try:
            await self.db.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    user_id TEXT NOT NULL, thread_id TEXT NOT NULL, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL,
                    last_checkpoint_id TEXT, last_run_id TEXT NOT NULL,
                    PRIMARY KEY (user_id, thread_id), UNIQUE (thread_id)
                )
            """)
            await self.db.execute("UPDATE sessions SET status='interrupted' WHERE status='running'")
            await self.db.commit()
        except BaseException:
            await self.db.close()
            raise
        return self

    async def __aexit__(self, *args):
        await self.db.close()

    async def get(self, user_id: str, thread_id: str) -> dict | None:
        async with self.db.execute(
            "SELECT * FROM sessions WHERE user_id=? AND thread_id=?", (user_id, thread_id),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def owner(self, thread_id: str) -> str | None:
        async with self.db.execute(
            "SELECT user_id FROM sessions WHERE thread_id=?", (thread_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return row["user_id"] if row else None

    async def list(self, user_id: str, limit: int) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM sessions WHERE user_id=? ORDER BY updated_at DESC, thread_id DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def start(self, user_id: str, thread_id: str, run_id: str, text: str):
        now = datetime.now(UTC).isoformat()
        async with self._write_lock:
            try:
                await self.db.execute("""
                    INSERT INTO sessions VALUES (?, ?, ?, ?, ?, 'running', NULL, ?)
                    ON CONFLICT(user_id, thread_id) DO UPDATE SET
                        updated_at=excluded.updated_at, status='running', last_run_id=excluded.last_run_id
                """, (user_id, thread_id, " ".join(text.split())[:100], now, now, run_id))
                await self.db.commit()
            except sqlite3.IntegrityError:
                await self.db.rollback()
                raise ValueError(
                    f"Session {thread_id} is not in profile {user_id}. "
                    "Choose an existing session for this profile or start a new one."
                ) from None

    async def finish(self, user_id: str, thread_id: str, status: str, checkpoint_id: str | None = None):
        async with self._write_lock:
            await self.db.execute("""
                UPDATE sessions SET status=?, updated_at=?,
                    last_checkpoint_id=COALESCE(?, last_checkpoint_id)
                WHERE user_id=? AND thread_id=?
            """, (status, datetime.now(UTC).isoformat(), checkpoint_id, user_id, thread_id))
            await self.db.commit()


def public_session(row: dict) -> dict:
    return {
        "threadId": row["thread_id"], "title": row["title"],
        "createdAt": row["created_at"], "updatedAt": row["updated_at"],
        "status": row["status"], "hasCheckpoint": bool(row["last_checkpoint_id"]),
    }
