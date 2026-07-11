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
"""Shared utilities for changping e2e tests.

This module re-exports key utilities from ``test_performance.py`` and adds
functional-guard-specific helpers (DB manipulation, sentinel detection,
experiment-not-created assertions).

Both ``test_performance.py`` and ``test_changping_functional.py`` import
from this module. The re-exports avoid circular dependencies while keeping
a single source of truth for setup logic.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Re-exports from test_performance.py
# ---------------------------------------------------------------------------
import test_performance as _tc  # noqa: F401

CHANGPING_DIR: Path = _tc.CHANGPING_DIR
CONFIG_DIR: Path = _tc.CONFIG_DIR
ORIGINAL_SQLITE_PATH: Path = _tc._ORIGINAL_SQLITE_PATH
MOCK_PORT: int = _tc._MOCK_PORT

disable_proxy_env = _tc._disable_proxy_env
start_mock_metavisor = _tc._start_mock_metavisor
stop_mock_metavisor = _tc._stop_mock_metavisor
build_cache_test_config = _tc._build_cache_test_config
resolve_session_root = _tc._resolve_session_root
auto_human_feedback = _tc.auto_human_feedback
extract_final_assistant_text = _tc._extract_final_assistant_text
find_created_experiment_id = _tc._find_created_experiment_id

QUERY_SEQUENCES: dict[str, dict[str, Any]] = _tc.QUERY_SEQUENCES
MODEL_PRESETS: dict[str, dict[str, str]] = _tc.MODEL_PRESETS

EXPECTED_BD55_1111_ANTIBODY_SAMPLE_ID: int = _tc.EXPECTED_BD55_1111_ANTIBODY_SAMPLE_ID
EXPECTED_XBB15_PSEUDOVIRUS_SAMPLE_ID: int = _tc.EXPECTED_XBB15_PSEUDOVIRUS_SAMPLE_ID
EXPECTED_HUH7_CELL_SAMPLE_ID: int = _tc.EXPECTED_HUH7_CELL_SAMPLE_ID
EXPECTED_NEW_EXPERIMENT_STATUS: str = _tc.EXPECTED_NEW_EXPERIMENT_STATUS

# Sentinel text markers (must match human_feedback.py)
SENTINEL_MARKER = "[SYSTEM]"
SENTINEL_EMPTY_FEEDBACK_MARKER = "未提供有效反馈"


# ---------------------------------------------------------------------------
# DB manipulation helpers (for functional guard tests)
# ---------------------------------------------------------------------------


def delete_wet_samples_row(db_path: Path, sample_id: int) -> int:
    """Delete a row from ``wet_samples`` by id, return rows deleted.

    Used by functional guard tests to simulate the data inconsistency
    (issue #5: pseudovirus_samples.id=904036 missing from wet_samples)
    and verify that HITL sentinel (issue #6) correctly prevents
    autonomous experiment creation.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("DELETE FROM wet_samples WHERE id = ?", (sample_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def delete_pseudovirus_sample(db_path: Path, sample_id: int) -> int:
    """Delete a row from ``pseudovirus_samples`` by id, return rows deleted.

    Unlike ``delete_wet_samples_row``, this removes the sample from the
    actual sample table, guaranteeing that no SQL path (with or without
    wet_samples JOIN) can find it. Use this when the test needs to
    reliably trigger the "pseudovirus not found" HITL scenario.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("DELETE FROM pseudovirus_samples WHERE id = ?", (sample_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def restore_wet_samples_row(db_path: Path, sample_id: int) -> None:
    """Restore a deleted wet_samples row for known pseudovirus samples.

    Currently only supports id=904036 (XBB.1.5 sample). If the row already
    exists, this is a no-op.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        existing = conn.execute("SELECT COUNT(*) FROM wet_samples WHERE id = ?", (sample_id,)).fetchone()[0]
        if existing > 0:
            return
        if sample_id == 904036:
            conn.execute(
                "INSERT INTO wet_samples (id, sample_id, create_time, status, freeze_count) "
                "VALUES (904036, 'PV-904036', '2025-12-17 09:05:00', 'STORED', 0)"
            )
            conn.commit()
        else:
            raise ValueError(f"restore_wet_samples_row: no known data for id={sample_id}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sentinel / HITL detection helpers
# ---------------------------------------------------------------------------


def check_sentinel_in_messages(messages: list[Any]) -> dict[str, Any]:
    """Check whether the HITL sentinel was triggered in the conversation.

    Returns a dict with:
      - ``sentinel_triggered``: bool — whether sentinel text was found
      - ``sentinel_count``: int — number of ToolMessages containing sentinel
      - ``sentinel_snippets``: list[str] — first 200 chars of each sentinel
    """
    if not isinstance(messages, list):
        return {"sentinel_triggered": False, "sentinel_count": 0, "sentinel_snippets": []}

    snippets: list[str] = []
    for msg in messages:
        msg_type = getattr(msg, "type", None) or ""
        if msg_type != "tool":
            continue
        content = getattr(msg, "content", "")
        if not isinstance(content, str):
            continue
        if SENTINEL_MARKER in content and SENTINEL_EMPTY_FEEDBACK_MARKER in content:
            snippets.append(content[:200])

    return {
        "sentinel_triggered": len(snippets) > 0,
        "sentinel_count": len(snippets),
        "sentinel_snippets": snippets,
    }


def check_experiment_exists(
    db_path: Path,
    cell_id: int,
    inhibitor_id: int,
    pseudovirus_id: int,
) -> int | None:
    """Check if an experiment with the given sample IDs exists in the DB.

    Returns the experiment id if found, else None.
    """
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT id FROM neutralization_experiments "
            "WHERE cell_sample_id = ? AND inhibitor_sample_id = ? AND pseudovirus_sample_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (cell_id, inhibitor_id, pseudovirus_id),
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None
    finally:
        conn.close()
