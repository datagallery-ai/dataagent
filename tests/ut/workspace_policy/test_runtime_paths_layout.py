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

from pathlib import Path

import pytest

from dataagent.utils.constants import DEFAULT_WORKSPACE_LAYOUT
from dataagent.utils.runtime_paths import (
    resolve_effective_workspace_root,
    resolve_job_subagents_root,
    resolve_jobs_root,
    resolve_layout_dir,
    resolve_session_root,
    resolve_worker_root,
    resolve_workspace_layout,
)


def test_resolve_workspace_layout_defaults_without_config() -> None:
    layout = resolve_workspace_layout(None)
    for key, value in DEFAULT_WORKSPACE_LAYOUT.items():
        assert getattr(layout, key) == value


def test_resolve_workspace_layout_merges_user_override() -> None:
    config = {"WORKSPACE_POLICY": {"layout": {"session_memory_dir": "state/mem"}}}
    layout = resolve_workspace_layout(config)
    assert layout.session_memory_dir == "state/mem"
    assert layout.context_dir == DEFAULT_WORKSPACE_LAYOUT["context_dir"]
    assert layout.workers_dir == DEFAULT_WORKSPACE_LAYOUT["workers_dir"]


@pytest.mark.parametrize(
    "segment,expected_suffix",
    [
        ("session_memory_dir", ".memory"),
        ("context_dir", ".context"),
        ("performance_dir", ".performance"),
        ("workers_dir", "workers"),
        ("subagents_dir", "subagents"),
        ("jobs_dir", "jobs"),
        ("runtime_dump_dir", ".runtime"),
        ("tool_outputs_dir", ".dataagent/tool_outputs"),
    ],
)
def test_resolve_layout_dir_each_segment(tmp_path: Path, segment: str, expected_suffix: str) -> None:
    workspace = tmp_path / "ws"
    got = resolve_layout_dir(workspace, segment)  # type: ignore[arg-type]
    assert got == (workspace / expected_suffix).resolve()


def test_resolve_layout_dir_rejects_unknown_segment(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown workspace layout segment"):
        resolve_layout_dir(tmp_path, "not_a_segment")  # type: ignore[arg-type]


def test_resolve_worker_root_uses_layout_workers_dir(tmp_path: Path) -> None:
    config = {"WORKSPACE_POLICY": {"layout": {"workers_dir": "swarm"}}}
    root = resolve_worker_root(
        user_id="u",
        parent_session_id="s",
        sub_id=7,
        parent_workspace=tmp_path / "custom-ws",
        config=config,
    )
    assert root == (tmp_path / "custom-ws" / "swarm" / "7").resolve()


def test_resolve_job_subagents_root_uses_layout_subagents_dir(tmp_path: Path) -> None:
    config = {"WORKSPACE_POLICY": {"layout": {"subagents_dir": ".dataagent/subagents"}}}
    root = resolve_job_subagents_root(parent_workspace=tmp_path / "custom-ws", config=config)
    assert root == (tmp_path / "custom-ws" / ".dataagent" / "subagents").resolve()


def test_resolve_jobs_root_uses_layout_jobs_dir(tmp_path: Path) -> None:
    config = {"WORKSPACE_POLICY": {"layout": {"jobs_dir": "state/jobs"}}}
    root = resolve_jobs_root(parent_workspace=tmp_path / "custom-ws", config=config)
    assert root == (tmp_path / "custom-ws" / "state" / "jobs").resolve()


def test_resolve_worker_root_parent_workspace_over_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "home"))
    custom = tmp_path / "configured-workspace"
    root = resolve_worker_root(
        user_id="u",
        parent_session_id="s",
        sub_id=1,
        parent_workspace=custom,
    )
    assert root == (custom / "workers" / "1").resolve()
    assert not str(root).startswith(str(tmp_path / "home"))


def test_resolve_effective_workspace_root_unchanged(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "home"))
    override = tmp_path / "override"
    got_override = resolve_effective_workspace_root(
        config=None,
        session_id="s1",
        user_id="u1",
        workspace_override=override,
    )
    assert got_override == override.resolve()

    configured = tmp_path / "yaml-ws"
    got_yaml = resolve_effective_workspace_root(
        config={"WORKSPACE": {"path": str(configured)}},
        session_id="s1",
        user_id="u1",
    )
    assert got_yaml == configured.resolve()

    got_default = resolve_effective_workspace_root(config=None, session_id="s1", user_id="u1")
    assert got_default == (tmp_path / "home" / "u1" / "s1").resolve()


def test_resolve_session_root_sanitizes_traversal_keeps_uuid(monkeypatch, tmp_path: Path) -> None:
    """session_id is one path component; uuid / alnum stay, traversal falls back."""
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "home"))
    user_root = (tmp_path / "home" / "u1").resolve()
    uuid_sid = "550e8400-e29b-41d4-a716-446655440000"
    assert resolve_session_root(session_id=uuid_sid, user_id="u1") == user_root / uuid_sid
    assert resolve_session_root(session_id="abc123", user_id="u1") == user_root / "abc123"
    escaped = resolve_session_root(session_id="../etc", user_id="u1")
    assert escaped == user_root / "default_session"
    assert escaped.is_relative_to(user_root)
