# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# ============================================================================
"""Unit tests for cooperative workspace session locks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dataagent.core.workspace.lock import (
    MAIN_SESSION_LOCK_TTL_SECONDS,
    WorkspaceBusyError,
    acquire_workspace_lock,
    is_workspace_lock_present,
    release_workspace_lock,
)


def test_acquire_busy_and_release(tmp_path: Path) -> None:
    """Second acquire on the same workspace root must fail until release."""
    workspace = tmp_path / "session_ws"
    workspace.mkdir()

    handle = acquire_workspace_lock(
        workspace_root=workspace,
        owner_kind="main_session",
        owner_id="session-a",
        purpose="flex_chat",
        ttl_seconds=MAIN_SESSION_LOCK_TTL_SECONDS,
    )
    assert handle is not None
    lock_file = handle.lock_dir / "lock.json"
    payload = json.loads(lock_file.read_text(encoding="utf-8"))
    assert payload["token"] == handle.token
    assert payload["owner_kind"] == "main_session"
    assert payload["owner_id"] == "session-a"
    assert payload["purpose"] == "flex_chat"
    assert is_workspace_lock_present(workspace)

    assert (
        acquire_workspace_lock(
            workspace_root=workspace,
            owner_kind="main_session",
            owner_id="session-b",
            purpose="flex_chat",
            ttl_seconds=MAIN_SESSION_LOCK_TTL_SECONDS,
        )
        is None
    )

    release_workspace_lock(handle)
    assert not handle.lock_dir.exists()
    assert not is_workspace_lock_present(workspace)

    second = acquire_workspace_lock(
        workspace_root=workspace,
        owner_kind="main_session",
        owner_id="session-b",
        purpose="flex_chat",
        ttl_seconds=MAIN_SESSION_LOCK_TTL_SECONDS,
    )
    assert second is not None
    release_workspace_lock(second)


def test_stale_lock_can_be_reacquired(tmp_path: Path) -> None:
    """Expired locks are cleaned so a new owner can acquire."""
    workspace = tmp_path / "session_ws"
    workspace.mkdir()
    lock_dir = workspace / ".lock"
    lock_dir.mkdir()
    expired = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    (lock_dir / "lock.json").write_text(
        json.dumps(
            {
                "token": "old-token",
                "owner_kind": "main_session",
                "owner_id": "dead",
                "purpose": "flex_chat",
                "expires_at": expired,
            }
        ),
        encoding="utf-8",
    )

    handle = acquire_workspace_lock(
        workspace_root=workspace,
        owner_kind="main_session",
        owner_id="alive",
        purpose="flex_chat",
        ttl_seconds=60,
    )
    assert handle is not None
    assert handle.owner_id == "alive"
    release_workspace_lock(handle)


def test_release_ignores_mismatched_token(tmp_path: Path) -> None:
    """Release must not delete a lock owned by another token."""
    workspace = tmp_path / "session_ws"
    workspace.mkdir()
    handle = acquire_workspace_lock(
        workspace_root=workspace,
        owner_kind="main_session",
        owner_id="session-a",
        purpose="flex_chat",
        ttl_seconds=60,
    )
    assert handle is not None
    forged = handle.__class__(
        workspace_root=handle.workspace_root,
        lock_dir=handle.lock_dir,
        token="not-the-owner",
        owner_kind=handle.owner_kind,
        owner_id=handle.owner_id,
        purpose=handle.purpose,
        expires_at=handle.expires_at,
    )
    release_workspace_lock(forged)
    assert handle.lock_dir.exists()
    release_workspace_lock(handle)


def test_workspace_busy_error_message_includes_owner(tmp_path: Path) -> None:
    """Busy error should surface readable owner metadata when lock.json is present."""
    workspace = tmp_path / "session_ws"
    workspace.mkdir()
    handle = acquire_workspace_lock(
        workspace_root=workspace,
        owner_kind="main_session",
        owner_id="session-a",
        purpose="flex_chat",
        ttl_seconds=60,
    )
    assert handle is not None
    err = WorkspaceBusyError.from_workspace(workspace)
    assert "session-a" in str(err)
    assert "flex_chat" in str(err)
    assert str(workspace / ".lock") in str(err)
    assert "确认占锁的 agent 已停止" in str(err)
    assert "删除锁可能导致多个 agent 并发写入 workspace" in str(err)
    release_workspace_lock(handle)


@pytest.mark.asyncio
async def test_data_agent_chat_rejects_busy_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DataAgent.chat must fail fast when the session workspace is already locked."""
    from dataagent.interface.sdk import agent as agent_module
    from dataagent.interface.sdk.agent import DataAgent

    workspace = tmp_path / "shared_ws"
    workspace.mkdir()
    existing = acquire_workspace_lock(
        workspace_root=workspace,
        owner_kind="main_session",
        owner_id="other",
        purpose="flex_chat",
        ttl_seconds=60,
    )
    assert existing is not None

    class _FakeConfig(dict):
        def get(self, key, default=None):
            if key == "AGENT_CONFIG.backend":
                return "langgraph"
            if key == "AGENT_CONFIG.type":
                return "react"
            if key == "USER_ID":
                return "u"
            if key == "SESSION_ID":
                return "s1"
            if isinstance(key, str) and "." in key:
                return default
            return super().get(key, default)

        def get_all(self):
            return {
                "USER_ID": "u",
                "SESSION_ID": "s1",
                "WORKSPACE": {"path": str(workspace)},
                "AGENT_CONFIG": {"backend": "langgraph", "type": "react"},
            }

        def copy(self):
            return _FakeConfig(dict(self))

    monkeypatch.setattr(agent_module.llm_manager, "init_from_config", lambda _cfg: None)
    agent = DataAgent(_FakeConfig())

    # Busy fails before the engine runs, so no engine mock is required.
    result = await agent.chat("hello", session_id="s1", workspace=workspace)
    assert "error" in result
    err = str(result["error"]).lower()
    assert "busy" in err
    assert "session-a" not in err  # owned by "other"
    assert "other" in str(result["error"])
    assert str(workspace / ".lock") in str(result["error"])
    release_workspace_lock(existing)
