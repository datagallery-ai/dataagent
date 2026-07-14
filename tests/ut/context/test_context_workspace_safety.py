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
from pathlib import Path

import pytest

from dataagent.core.context.context import (
    ContextFactory,
    ContextInitOptions,
    build_context_init_options,
)
from dataagent.core.flex.utils.context_from_state import get_context_for_flex_state
from dataagent.utils.runtime_paths import resolve_effective_workspace_root


class _ConfigManager:
    def __init__(self, settings: dict):
        self._settings = settings

    def get(self, key: str, default=None):
        return self._settings.get(key, default)

    def get_all(self):
        return dict(self._settings)


class _Runtime:
    def __init__(self, config_manager: _ConfigManager):
        self.config_manager = config_manager


def test_build_context_init_options_accepts_workspace_under_configured_root(tmp_path: Path) -> None:
    root = (tmp_path / "allowed").resolve()
    child = root / "child"
    settings = {
        "USER_ID": "u",
        "SESSION_ID": "s",
        "WORKSPACE": {"path": str(root)},
    }

    options = build_context_init_options(_ConfigManager(settings), workspace=child)

    assert options.workspace == child.resolve()


def test_build_context_init_options_rejects_workspace_outside_configured_root(tmp_path: Path) -> None:
    root = (tmp_path / "allowed").resolve()
    outside = (tmp_path / "outside").resolve()
    settings = {
        "USER_ID": "u",
        "SESSION_ID": "s",
        "WORKSPACE": {"path": str(root)},
    }

    with pytest.raises(ValueError, match="workspace"):
        build_context_init_options(_ConfigManager(settings), workspace=outside)


def test_build_context_init_options_accepts_dynamic_session_workspace_without_workspace_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without WORKSPACE.path, framework-derived dynamic session dirs must pass (REST/CLI)."""
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "home"))
    runtime_session = "20260714_174512_abcdef12-3456-7890-abcd-ef1234567890"
    settings = {
        "USER_ID": "anonymous",
        # Static config default differs from the runtime-generated session id.
        "SESSION_ID": "default_session",
    }
    workspace = resolve_effective_workspace_root(
        config=settings,
        user_id="anonymous",
        session_id=runtime_session,
    )

    options = build_context_init_options(_ConfigManager(settings), workspace=workspace)

    assert options.workspace == workspace.resolve()


def test_context_factory_rejects_escape_when_workspace_path_configured(tmp_path: Path) -> None:
    """Context.__init__ enforces the cage even when ContextInitOptions is built directly."""
    ContextFactory.clear_context()
    root = (tmp_path / "allowed").resolve()
    root.mkdir()
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()

    with pytest.raises(ValueError, match="workspace"):
        ContextFactory.get_context(
            user_id="anonymous",
            session_id="s1",
            run_id=0,
            sub_id=0,
            options=ContextInitOptions(
                workspace=outside,
                config={"WORKSPACE": {"path": str(root)}},
            ),
        )


def test_context_factory_accepts_workspace_without_workspace_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ContextFactory.clear_context()
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "home"))
    workspace = (tmp_path / "session_ws").resolve()
    workspace.mkdir()

    ctx = ContextFactory.get_context(
        user_id="anonymous",
        session_id="20260714_runtime_session",
        run_id=0,
        sub_id=0,
        options=ContextInitOptions(workspace=workspace, config={"SESSION_ID": "default_session"}),
    )

    assert Path(ctx.state.workspace).resolve() == workspace


def test_get_context_for_flex_state_rejects_escape_when_workspace_path_configured(
    tmp_path: Path,
) -> None:
    ContextFactory.clear_context()
    root = (tmp_path / "allowed").resolve()
    root.mkdir()
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    settings = {"WORKSPACE": {"path": str(root)}}
    state = {
        "user_id": "anonymous",
        "session_id": "s1",
        "run_id": 0,
        "sub_id": 0,
        "workspace": outside,
    }

    with pytest.raises(ValueError, match="workspace"):
        get_context_for_flex_state(state, _Runtime(_ConfigManager(settings)))


def test_get_context_for_flex_state_accepts_dynamic_session_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ContextFactory.clear_context()
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "home"))
    runtime_session = "20260714_flex_state_ok"
    settings = {"USER_ID": "anonymous", "SESSION_ID": "default_session"}
    workspace = resolve_effective_workspace_root(
        config=settings,
        user_id="anonymous",
        session_id=runtime_session,
    )
    state = {
        "user_id": "anonymous",
        "session_id": runtime_session,
        "run_id": 0,
        "sub_id": 0,
        "workspace": workspace,
    }

    ctx = get_context_for_flex_state(state, _Runtime(_ConfigManager(settings)))

    assert ctx is not None
    assert Path(ctx.state.workspace).resolve() == workspace.resolve()
