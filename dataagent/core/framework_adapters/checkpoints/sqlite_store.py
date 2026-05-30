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
import sqlite3
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from dataagent.core.framework_adapters.checkpoints.types import CheckpointRecord


class SqliteCheckpointStore:
    """
    SQLite checkpoint store（用于 openjiuwen human_feedback “中断-恢复”）。

    目标：
    - 与 PostgresCheckpointStore 保持一致：save()/load()
    - 以单文件 sqlite 方式持久化（适合本地/轻量部署）
    """

    def __init__(self, sqlite_path: str, *, table_name: str = "dataagent_checkpoints"):
        self._path = str(sqlite_path)
        self._table = str(table_name)
        self._ensure_parent_dir()
        self._ensure_table()

    def save(self, *, start_at: str, interrupt_message: str, state: dict[str, Any]) -> str:
        """生成 UUID，将状态序列化后保存到数据库，并返回检查点 ID。"""
        checkpoint_id = uuid4().hex
        payload = json.dumps(state, ensure_ascii=False, default=str)
        sql = f"""
        INSERT INTO {self._table} (checkpoint_id, start_at, interrupt_message, state)
        VALUES (?, ?, ?, ?)
        """
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, (checkpoint_id, str(start_at), str(interrupt_message), payload))
            conn.commit()
        return checkpoint_id

    def load(self, checkpoint_id: str) -> CheckpointRecord:
        """从数据库加载指定 ID 的检查点，处理状态数据的反序列化并返回记录。"""
        sql = f"""
        SELECT checkpoint_id, start_at, interrupt_message, state
        FROM {self._table}
        WHERE checkpoint_id = ?
        """
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, (str(checkpoint_id),))
            row = cur.fetchone()
        if not row:
            raise FileNotFoundError(f"Checkpoint not found in sqlite: {checkpoint_id}")
        cid, start_at, interrupt_message, raw_state = row

        state_obj: dict[str, Any] = {}

        state_text: str = ""
        if isinstance(raw_state, (bytes, bytearray)):
            with suppress(Exception):
                state_text = raw_state.decode("utf-8", errors="ignore")
        elif isinstance(raw_state, str):
            state_text = raw_state
        elif isinstance(raw_state, dict):
            # 兜底：sqlite3 通常返回 str，但保留对特殊 row_factory 的兼容
            state_obj = dict(raw_state)

        if state_text:
            try:
                parsed = json.loads(state_text)
                if isinstance(parsed, dict):
                    state_obj = parsed
            except Exception:
                state_obj = {}
        return CheckpointRecord(
            checkpoint_id=str(cid or checkpoint_id),
            start_at=str(start_at or ""),
            interrupt_message=str(interrupt_message or ""),
            state=state_obj,
        )

    def _ensure_parent_dir(self) -> None:
        # :memory: 不需要目录
        if self._path == ":memory:":
            return
        p = Path(self._path)
        if p.parent and str(p.parent) not in (".", ""):
            p.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        # timeout 避免并发写入时立刻报 database is locked
        conn = sqlite3.connect(self._path, timeout=30, check_same_thread=False)
        return conn

    def _ensure_table(self) -> None:
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {self._table} (
          checkpoint_id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
          start_at TEXT NOT NULL,
          interrupt_message TEXT NOT NULL,
          state TEXT NOT NULL
        );
        """
        with self._connect() as conn:
            cur = conn.cursor()
            # WAL 对 sqlite 并发更友好（在文件库上有效；内存库会忽略/报错均可接受）
            with suppress(Exception):
                cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute(ddl)
            conn.commit()
