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

import sqlite3
from pathlib import Path
from typing import Any

from dataagent.actions.gym.nl2sql.base_env import BaseNL2SQLEnv


class SQLiteEnv(BaseNL2SQLEnv):
    """
    SQLiteEnv environment wrapper
    """

    def __init__(
        self,
        table_metadata_table: str = "table_metadata",
        column_metadata_table: str = "column_metadata",
        config_manager: Any | None = None,
    ) -> None:
        """
        Initialize SQLite NL2SQL gym env.

        Args:
            table_metadata_table: Metadata table name.
            column_metadata_table: Column metadata table name.
            config_manager: Per-Agent ConfigManager for ``DATABASE.config.path``.
        """
        super().__init__(
            self._resolve_db_path(config_manager),
            table_metadata_table,
            column_metadata_table,
        )

    @property
    def conn(self):
        """Ensure connection to the database."""
        if self._conn is None:
            self._conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro&immutable=1",
                uri=True,
                check_same_thread=False,
            )
        return self._conn

    @staticmethod
    def _resolve_db_path(config_manager: Any | None) -> str:
        """Read DB path from per-Agent YAML ``DATABASE.config.path``."""
        if config_manager is None:
            raise RuntimeError("SQLiteEnv requires per-Agent config_manager for DATABASE.config.path.")
        raw = str(config_manager.get("DATABASE.config.path", "") or "").strip()
        if not raw:
            raise ValueError("SQLiteEnv requires DATABASE.config.path in the agent YAML.")
        return str(Path(raw).expanduser())

    def _table_exists(self, name: str) -> bool:
        """Check if a table exists in the SQLite database."""
        try:
            return (
                self._execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                    (name,),
                ).fetchone()
                is not None
            )
        except Exception:
            return False
