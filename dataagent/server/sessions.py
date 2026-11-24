"""A small durable session index. Conversation state belongs to LangGraph."""

import asyncio
import sqlite3
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


def _acquire_workspace_lock(handle) -> None:
    if sys.platform == "win32":
        # msvcrt locks a byte range from the current offset.
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("Another DataAgent backend owns this workspace") from None
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("Another DataAgent backend owns this workspace") from None


def _release_workspace_lock(handle) -> None:
    if sys.platform == "win32":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def workspace_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_dir / "backend.lock"
    # Windows byte-range locks need an existing byte; r+b avoids append-mode seek limits.
    if not lock_path.exists() or lock_path.stat().st_size == 0:
        lock_path.write_bytes(b"\0")
    with lock_path.open("r+b") as handle:
        _acquire_workspace_lock(handle)
        try:
            yield
        finally:
            _release_workspace_lock(handle)


def pending_interactions(snapshot):
    pending = []
    for task in snapshot.tasks:
        # A completed parallel task retains its earlier interrupt write until
        # the whole superstep commits. A resume-only write yields {}, so it is
        # not a completed result (e.g. an invalid answer that interrupts again).
        if task.result:
            continue
        for item in task.interrupts:
            value = item.value
            if not isinstance(value, dict) or value.get("type") != "human_feedback":
                continue
            if not isinstance(value.get("message"), str):
                continue
            entry = {"type": "human_feedback", "interruptId": item.id, "message": value["message"]}
            if value.get("options"):
                entry["options"] = value["options"]
            if value.get("validation_error"):
                entry["validationError"] = value["validation_error"]
            pending.append(entry)
    return pending


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
            await self.db.execute(
                "CREATE TABLE IF NOT EXISTS submitted_runs (thread_id TEXT, run_id TEXT, PRIMARY KEY(thread_id, run_id))"
            )
            await self.db.execute("UPDATE sessions SET status='interrupted' WHERE status='running'")
            # Rows written before the status was renamed; one value, one meaning.
            await self.db.execute("UPDATE sessions SET status='completed' WHERE status='complete'")
            await self.db.commit()
        except BaseException:
            await self.db.close()
            raise
        return self

    async def __aexit__(self, *args):
        await self.db.close()

    async def get(self, user_id: str, thread_id: str) -> dict | None:
        async with self.db.execute(
            "SELECT * FROM sessions WHERE user_id=? AND thread_id=?",
            (user_id, thread_id),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def owner(self, thread_id: str) -> str | None:
        async with self.db.execute(
            "SELECT user_id FROM sessions WHERE thread_id=?",
            (thread_id,),
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
                await self.db.execute(
                    "INSERT INTO submitted_runs VALUES (?, ?)", (thread_id, run_id)
                )
                await self.db.execute(
                    """
                    INSERT INTO sessions VALUES (?, ?, ?, ?, ?, 'running', NULL, ?)
                    ON CONFLICT(user_id, thread_id) DO UPDATE SET
                        updated_at=excluded.updated_at, status='running', last_run_id=excluded.last_run_id
                """,
                    (user_id, thread_id, " ".join(text.split())[:100], now, now, run_id),
                )
                await self.db.commit()
            except sqlite3.IntegrityError:
                await self.db.rollback()
                raise ValueError(
                    f"Run was already submitted or session {thread_id} does not belong to user {user_id}. "
                    "Choose one of this user's sessions or start a new one."
                ) from None

    async def finish(
        self, user_id: str, thread_id: str, status: str, checkpoint_id: str | None = None
    ):
        async with self._write_lock:
            await self.db.execute(
                """
                UPDATE sessions SET status=?, updated_at=?,
                    last_checkpoint_id=COALESCE(?, last_checkpoint_id)
                WHERE user_id=? AND thread_id=?
            """,
                (status, datetime.now(UTC).isoformat(), checkpoint_id, user_id, thread_id),
            )
            await self.db.commit()


class SessionTurn:
    """One GUI or TUI turn against the shared session index.

    Cancellation leaves ``status`` as ``interrupted`` so ``release`` matches the
    terminal route. A successful checkpoint is written only through ``complete``.
    A stop or a model error stores the checkpoint the graph actually reached,
    so the next turn reads the same messages the transcript shows.
    """

    def __init__(self, sessions: Sessions, user_id: str, thread_id: str):
        self.sessions = sessions
        self.user_id = user_id
        self.thread_id = thread_id
        self.status = "interrupted"

    async def start(self, run_id: str, text: str) -> None:
        await self.sessions.start(self.user_id, self.thread_id, run_id, text)

    async def complete(self, checkpoint_id: str) -> None:
        self.status = "completed"
        await self.sessions.finish(self.user_id, self.thread_id, "completed", checkpoint_id)

    async def release(self) -> None:
        if self.status != "completed":
            await self.sessions.finish(self.user_id, self.thread_id, self.status)

    async def settle(self, graph, *, complete: bool = False):
        """Point the session row at the checkpoint this turn reached.

        ``complete`` accepts a completed graph or a supported HITL pause.
        Other pending work is an error.
        A turn that stored no messages leaves the previous checkpoint in place.
        """
        latest = await graph.aget_state({"configurable": {"thread_id": self.thread_id}})
        configurable = (latest.config or {}).get("configurable") or {}
        checkpoint_id = configurable.get("checkpoint_id")
        if complete:
            if pending_interactions(latest):
                self.status = "waiting_for_input"
                await self.sessions.finish(self.user_id, self.thread_id, self.status, checkpoint_id)
                return latest
            if latest.next:
                raise RuntimeError("Agent stopped with unfinished work or an unsupported interrupt")
            await self.complete(latest.config["configurable"]["checkpoint_id"])
            return latest
        if checkpoint_id and (latest.values or {}).get("messages"):
            await self.sessions.finish(
                self.user_id,
                self.thread_id,
                self.status,
                checkpoint_id,
            )
        return latest


def continuation_configurable(thread_id: str, row: dict | None) -> dict:
    """Graph address for a load and the next turn. A saved checkpoint wins; otherwise the thread head."""
    configurable = {"thread_id": thread_id}
    checkpoint_id = row.get("last_checkpoint_id") if row else None
    if checkpoint_id:
        configurable["checkpoint_id"] = checkpoint_id
    return configurable


def public_session(row: dict) -> dict:
    return {
        "threadId": row["thread_id"],
        "title": row["title"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "status": row["status"],
        "hasCheckpoint": bool(row["last_checkpoint_id"]),
    }
