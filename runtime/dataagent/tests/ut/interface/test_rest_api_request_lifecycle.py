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
"""Regression tests for stateless REST request resources."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from dataagent.core.context.context import ContextFactory, ContextInitOptions
from dataagent.interface.rest_api.service import DataAgentService


def _nl2sql_service() -> DataAgentService:
    """Return an NL2SQL service configured without a persistent workspace."""
    service = DataAgentService()
    service._cached_agent_type = "nl2sql"
    service._agent = SimpleNamespace(
        config={"USER_ID": "anonymous", "RUN_ID": 0, "SUB_ID": 0, "WORKSPACE": {}},
    )
    return service


def test_request_scope_leaves_configured_workspace_to_sdk_lifecycle(tmp_path: Path) -> None:
    """REST must not replace or delete a persistent workspace configured for the SDK."""
    workspace = tmp_path / "persistent-workspace"
    workspace.mkdir()
    marker = workspace / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    service = _nl2sql_service()
    service._agent.config.update({"WORKSPACE": {"path": str(workspace)}})

    with service._request_scope() as request:
        assert request.get("session_id")
        assert request.get("workspace") is None

    assert marker.read_text(encoding="utf-8") == "keep"


def test_request_scope_releases_context_and_removes_ephemeral_workspace() -> None:
    """A stateless REST request must not retain Context or workspace directories."""
    ContextFactory.clear_context()
    service = _nl2sql_service()

    with service._request_scope() as request:
        session_id = str(request.get("session_id"))
        workspace = Path(request.get("workspace"))
        ContextFactory.get_context(
            user_id="anonymous",
            session_id=session_id,
            run_id=0,
            sub_id=0,
            options=ContextInitOptions(workspace=workspace),
        )
        assert workspace.is_dir()

    assert not workspace.exists()
    assert ContextFactory.release_context(user_id="anonymous", session_id=session_id, run_id=0, sub_id=0) == 0
    ContextFactory.clear_context()


def test_request_scope_removes_ephemeral_workspace_after_exception() -> None:
    """The ephemeral workspace must be removed when request processing raises."""
    service = _nl2sql_service()
    workspace: Path | None = None

    with pytest.raises(RuntimeError, match="request failed"), service._request_scope() as request:
        workspace = Path(request.get("workspace"))
        (workspace / "partial.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("request failed")

    assert workspace is not None
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_concurrent_request_scopes_use_distinct_ephemeral_workspaces() -> None:
    """Concurrent REST requests must neither share nor retain their ephemeral workspaces."""
    service = _nl2sql_service()
    request_count = 16
    all_entered = asyncio.Event()
    lock = asyncio.Lock()
    entered = 0
    workspaces: list[Path] = []
    session_ids: list[str] = []

    async def run_one(index: int) -> None:
        """Hold one request scope open until all concurrent scopes have been created."""
        nonlocal entered
        with service._request_scope() as request:
            workspace = Path(request.get("workspace"))
            marker = workspace / "marker.txt"
            marker.write_text(str(index), encoding="utf-8")
            workspaces.append(workspace)
            session_ids.append(str(request.get("session_id")))
            async with lock:
                entered += 1
                if entered == request_count:
                    all_entered.set()
            await all_entered.wait()
            assert marker.read_text(encoding="utf-8") == str(index)
        assert not workspace.exists()

    await asyncio.gather(*(run_one(index) for index in range(request_count)))

    assert len(set(workspaces)) == request_count
    assert len(set(session_ids)) == request_count
    assert all(not workspace.exists() for workspace in workspaces)
