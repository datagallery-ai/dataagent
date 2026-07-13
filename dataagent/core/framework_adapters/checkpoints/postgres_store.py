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
import re
from typing import Any
from uuid import uuid4

from dataagent.core.framework_adapters.checkpoints.types import CheckpointRecord

# SQL table names are interpolated, so allow safe identifiers only.
_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_table_name(table_name: str) -> str:
    name = str(table_name)
    if not _TABLE_NAME_RE.fullmatch(name):
        raise ValueError("Invalid checkpoint table name")
    return name


class PostgresCheckpointStore:
    """
    Postgres checkpoint store（用于 openjiuwen human_feedback “中断-恢复”）。

    约束（按你的要求）：
    - openjiuwen 只使用 Postgres 存储 checkpoint，不再使用文件存储
    - 接口与 FileCheckpointStore 保持一致：save()/load()
    """

    def __init__(self, dsn: str, *, table_name: str = "dataagent_checkpoints"):
        self._dsn = str(dsn)
        self._table = _validate_table_name(table_name)
        self._ensure_table()

    def load(self, checkpoint_id: str) -> CheckpointRecord:
        """从数据库加载指定 ID 的检查点，处理状态数据的反序列化并返回记录。"""
        sql = f"""
        SELECT checkpoint_id, start_at, interrupt_message, state
        FROM {self._table}
        WHERE checkpoint_id = %s
        """
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, (str(checkpoint_id),))
            row = cur.fetchone()
        if not row:
            raise FileNotFoundError(f"Checkpoint not found in postgres: {checkpoint_id}")
        cid, start_at, interrupt_message, state = row
        # psycopg2/3 可能返回 dict 或 str，统一转 dict
        if isinstance(state, str):
            try:
                state_obj = json.loads(state)
            except Exception:
                state_obj = {}
        elif isinstance(state, dict):
            state_obj = state
        else:
            try:
                state_obj = dict(state)  # type: ignore[arg-type]
            except Exception:
                state_obj = {}
        return CheckpointRecord(
            checkpoint_id=str(cid or checkpoint_id),
            start_at=str(start_at or ""),
            interrupt_message=str(interrupt_message or ""),
            state=state_obj if isinstance(state_obj, dict) else {},
        )

    def _connect(self):
        """建立数据库连接，优先尝试 psycopg (v3)，失败则降级使用 psycopg2。"""
        # psycopg3 优先；没有则尝试 psycopg2
        try:
            import psycopg  # type: ignore[import-not-found]

            return psycopg.connect(self._dsn)
        except Exception:
            import psycopg2  # type: ignore[import-not-found]

            return psycopg2.connect(self._dsn)

    def _ensure_table(self) -> None:
        """检查并初始化数据库表结构，如果表不存在则创建。"""
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {self._table} (
          checkpoint_id TEXT PRIMARY KEY,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          start_at TEXT NOT NULL,
          interrupt_message TEXT NOT NULL,
          state JSONB NOT NULL
        );
        """
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(ddl)
            conn.commit()

    def save(self, *, start_at: str, interrupt_message: str, state: dict[str, Any]) -> str:
        """生成 UUID，将状态序列化后保存到数据库，并返回检查点 ID。"""
        checkpoint_id = uuid4().hex
        payload = json.dumps(state, ensure_ascii=False, default=str)
        sql = f"""
        INSERT INTO {self._table} (checkpoint_id, start_at, interrupt_message, state)
        VALUES (%s, %s, %s, %s::jsonb)
        """
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(sql, (checkpoint_id, str(start_at), str(interrupt_message), payload))
            conn.commit()
        return checkpoint_id
