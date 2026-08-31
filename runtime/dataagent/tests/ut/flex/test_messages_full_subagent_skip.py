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

import pytest
from langchain_core.messages import HumanMessage

from dataagent.core.flex.hooks.history_writer import save_messages_full_for_state
from dataagent.utils.runtime_paths import FLEX_PERSISTENCE_ROOT_ENV


def test_save_messages_full_for_state_writes_main_agent(tmp_path) -> None:
    """Main agent (sub_id=0) persists audit lines under session memory."""
    workspace = tmp_path / "parent_session"
    workspace.mkdir()

    save_messages_full_for_state(
        {
            "user_id": "u1",
            "session_id": "parent_session",
            "sub_id": 0,
            "workspace": workspace,
            "messages": [],
        },
        [HumanMessage(content="main audit")],
    )

    full_path = workspace / ".memory" / "messages_full.json"
    assert full_path.is_file()
    assert "main audit" in full_path.read_text(encoding="utf-8")


def test_save_messages_full_for_state_skips_wrapped_subagent_on_parent_workspace(tmp_path) -> None:
    """Wrapped subagent sharing parent workspace must not append to parent messages_full."""
    workspace = tmp_path / "parent_session"
    mem_dir = workspace / ".memory"
    mem_dir.mkdir(parents=True)
    existing = mem_dir / "messages_full.json"
    existing.write_text(
        '{"messages": [{"type": "HumanMessage", "content": "parent-only", '
        '"name": "", "additional_kwargs": {}, "response_metadata": {}}]}',
        encoding="utf-8",
    )

    save_messages_full_for_state(
        {
            "user_id": "u1",
            "session_id": "subagent_parent_session_365243",
            "sub_id": 365243,
            "workspace": workspace,
            "messages": [],
        },
        [HumanMessage(content="subagent audit must not appear")],
    )

    content = existing.read_text(encoding="utf-8")
    assert "parent-only" in content
    assert "subagent audit must not appear" not in content


def test_save_messages_full_for_state_writes_job_subagent_workspace(tmp_path, monkeypatch) -> None:
    """Job-path subagent writes audit log under subagents/{id}/.memory/."""
    parent = tmp_path / "parent_session"
    parent.mkdir()
    subagent_ws = parent / "subagents" / "fb0adaefdcd74db7b0a2b7f35bed5747"
    subagent_ws.mkdir(parents=True)
    monkeypatch.setenv(FLEX_PERSISTENCE_ROOT_ENV, str(subagent_ws))

    save_messages_full_for_state(
        {
            "user_id": "u1",
            "session_id": "fb0adaefdcd74db7b0a2b7f35bed5747",
            "sub_id": 669015,
            "workspace": subagent_ws,
            "messages": [],
        },
        [HumanMessage(content="job subagent audit")],
    )

    full_path = subagent_ws / ".memory" / "messages_full.json"
    assert full_path.is_file()
    assert "job subagent audit" in full_path.read_text(encoding="utf-8")
    assert not (parent / ".memory" / "messages_full.json").exists()
