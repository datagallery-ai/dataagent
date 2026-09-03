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
"""Regression tests for native REST request session scoping."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from dataagent.interface.rest_api.service import DataAgentService


def _nl2sql_service() -> DataAgentService:
    """Return an NL2SQL service using the SDK-managed workspace lifecycle."""
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


def test_request_scope_assigns_a_distinct_native_session() -> None:
    """Each stateless NL2SQL REST request should receive a distinct LangGraph session."""
    service = _nl2sql_service()

    session_ids = []
    for _ in range(16):
        with service._request_scope() as request:
            session_ids.append(str(request.get("session_id", "")))
            assert set(request) == {"session_id"}

    assert all(session_ids)
    assert len(set(session_ids)) == len(session_ids)


def test_request_scope_remains_usable_after_exception() -> None:
    """An exception in one request must not leak or reuse its native session id."""
    service = _nl2sql_service()
    failed_session_id = ""

    with pytest.raises(RuntimeError, match="request failed"), service._request_scope() as request:
        failed_session_id = str(request.get("session_id", ""))
        raise RuntimeError("request failed")

    with service._request_scope() as request:
        next_session_id = str(request.get("session_id", ""))

    assert failed_session_id
    assert next_session_id
    assert next_session_id != failed_session_id
