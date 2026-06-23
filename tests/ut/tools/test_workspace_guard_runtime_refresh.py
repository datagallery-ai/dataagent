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
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from dataagent.actions.tools.local_tool.sandbox import BubblewrapSandbox, NoopSandbox

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
FLEX_AGENT_PATH = WORKSPACE_ROOT / "dataagent" / "core" / "flex" / "agent.py"


def _load_module(module_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_refresh_workspace_runtime_context_rebuilds_sandbox_from_current_user_and_workspace(tmp_path: Path):
    flex_agent_module = _load_module("flex_agent_runtime_refresh_test", FLEX_AGENT_PATH)

    workspace = (tmp_path / "workspace-current").resolve()
    old_workspace = (tmp_path / "workspace-old").resolve()
    old_skill_root = (tmp_path / "old-skill").resolve()
    new_skill_root = (tmp_path / "new-skill").resolve()
    allow_root = (tmp_path / "allow").resolve()

    runtime = SimpleNamespace(
        workspace_dir=workspace,
        sandbox=NoopSandbox(
            workspace_root=old_workspace,
            skill_aliases={"old": old_skill_root},
        ),
        set_sandbox=lambda sb: setattr(runtime, "sandbox", sb),
    )
    agent = object.__new__(flex_agent_module.FlexAgent)
    agent.config = {"WORKSPACE": {"allow_path": [str(allow_root)]}}

    refreshed_users: list[str | None] = []

    class _ToolManagerStub:
        @staticmethod
        def refresh_user_skills(*, user_id: str | None = None):
            refreshed_users.append(user_id)

        @staticmethod
        def list_skills():
            return [{"name": "pdf", "path": str(new_skill_root)}]

        @staticmethod
        def workspace_allow_path_list(config):
            return [str(allow_root)]

    agent.env_config = SimpleNamespace(tool_manager=_ToolManagerStub())
    state = {"user_id": "user-123", "workspace": str(workspace)}
    agent._refresh_workspace_runtime_context(state, runtime)

    sb = runtime.sandbox
    assert refreshed_users == ["user-123"]
    assert sb.workspace_root == workspace
    assert sb.skill_aliases == {"pdf": new_skill_root}
    assert sb.allow_read_roots == [allow_root]


def test_refresh_workspace_runtime_context_sandbox_disabled_env_forces_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    flex_agent_module = _load_module("flex_agent_runtime_refresh_disabled_env_test", FLEX_AGENT_PATH)

    workspace = (tmp_path / "workspace-current").resolve()
    allow_root = (tmp_path / "allow").resolve()

    runtime = SimpleNamespace(
        workspace_dir=workspace,
        sandbox=NoopSandbox(workspace_root=workspace),
        set_sandbox=lambda sb: setattr(runtime, "sandbox", sb),
    )
    agent = object.__new__(flex_agent_module.FlexAgent)
    agent.config = {"WORKSPACE": {"allow_path": [str(allow_root)]}}

    class _ToolManagerStub:
        @staticmethod
        def refresh_user_skills(*, user_id: str | None = None):
            return None

        @staticmethod
        def list_skills():
            return []

        @staticmethod
        def workspace_allow_path_list(config):
            return [str(allow_root)]

    agent.env_config = SimpleNamespace(tool_manager=_ToolManagerStub())
    monkeypatch.delenv("DATAAGENT_SANDBOX_ENABLED", raising=False)
    monkeypatch.setenv("DATAAGENT_SANDBOX_ENABLED", "false")
    with patch("dataagent.actions.tools.local_tool.sandbox.shutil.which", return_value="/usr/bin/bwrap"):
        agent._refresh_workspace_runtime_context({"user_id": "user-123", "workspace": str(workspace)}, runtime)

    assert isinstance(runtime.sandbox, NoopSandbox)


def test_refresh_workspace_runtime_context_sandbox_enabled_env_uses_bwrap_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    flex_agent_module = _load_module("flex_agent_runtime_refresh_enabled_env_test", FLEX_AGENT_PATH)

    workspace = (tmp_path / "workspace-current").resolve()

    runtime = SimpleNamespace(
        workspace_dir=workspace,
        sandbox=NoopSandbox(workspace_root=workspace),
        set_sandbox=lambda sb: setattr(runtime, "sandbox", sb),
    )
    agent = object.__new__(flex_agent_module.FlexAgent)
    agent.config = {"WORKSPACE": {"allow_path": []}}

    class _ToolManagerStub:
        @staticmethod
        def refresh_user_skills(*, user_id: str | None = None):
            return None

        @staticmethod
        def list_skills():
            return []

        @staticmethod
        def workspace_allow_path_list(config):
            return []

    agent.env_config = SimpleNamespace(tool_manager=_ToolManagerStub())
    monkeypatch.delenv("DATAAGENT_SANDBOX_ENABLED", raising=False)
    monkeypatch.setenv("DATAAGENT_SANDBOX_ENABLED", "true")
    with patch("dataagent.actions.tools.local_tool.sandbox.is_bwrap_sandbox_usable", return_value=True):
        agent._refresh_workspace_runtime_context({"user_id": "user-123", "workspace": str(workspace)}, runtime)

    assert isinstance(runtime.sandbox, BubblewrapSandbox)


def test_refresh_workspace_runtime_context_sandbox_enabled_env_falls_back_when_bwrap_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    flex_agent_module = _load_module("flex_agent_runtime_refresh_enabled_env_no_bwrap_test", FLEX_AGENT_PATH)

    workspace = (tmp_path / "workspace-current").resolve()

    runtime = SimpleNamespace(
        workspace_dir=workspace,
        sandbox=NoopSandbox(workspace_root=workspace),
        set_sandbox=lambda sb: setattr(runtime, "sandbox", sb),
    )
    agent = object.__new__(flex_agent_module.FlexAgent)
    agent.config = {"WORKSPACE": {"allow_path": []}}

    class _ToolManagerStub:
        @staticmethod
        def refresh_user_skills(*, user_id: str | None = None):
            return None

        @staticmethod
        def list_skills():
            return []

        @staticmethod
        def workspace_allow_path_list(config):
            return []

    agent.env_config = SimpleNamespace(tool_manager=_ToolManagerStub())
    monkeypatch.delenv("DATAAGENT_SANDBOX_ENABLED", raising=False)
    monkeypatch.setenv("DATAAGENT_SANDBOX_ENABLED", "true")
    with patch("dataagent.actions.tools.local_tool.sandbox.is_bwrap_sandbox_usable", return_value=False):
        agent._refresh_workspace_runtime_context({"user_id": "user-123", "workspace": str(workspace)}, runtime)

    assert isinstance(runtime.sandbox, NoopSandbox)


@pytest.mark.asyncio
async def test_chat_refreshes_runtime_sandbox_before_workflow_invocation(tmp_path: Path):
    flex_agent_module = _load_module("flex_agent_runtime_refresh_chat_test", FLEX_AGENT_PATH)

    workspace = (tmp_path / "chat-workspace").resolve()
    old_workspace = (tmp_path / "old-workspace").resolve()
    new_skill_root = (tmp_path / "new-skill").resolve()
    allow_root = (tmp_path / "allow").resolve()

    runtime = SimpleNamespace(
        workspace_dir=workspace,
        hierarchy=None,
        sandbox=NoopSandbox(workspace_root=old_workspace),
        update_from_state=lambda state: setattr(
            runtime, "workspace_dir", Path(str(state.get("workspace") or runtime.workspace_dir)).resolve()
        ),
        set_sandbox=lambda sb: setattr(runtime, "sandbox", sb),
        reset_flex_planner_user_sync=lambda: None,
        on_subagent_progress=None,
    )

    captured = {}

    class _BackendStub:
        def set_runtime(self, runtime_obj):
            captured["runtime_set"] = runtime_obj

        async def ainvoke(self, state):
            sb = runtime.sandbox
            captured["state"] = dict(state)
            captured["workspace_root"] = sb.workspace_root
            captured["skill_aliases"] = dict(sb.skill_aliases)
            captured["allow_read_roots"] = list(sb.allow_read_roots)
            return {"messages": [], "complete": True}

    agent = object.__new__(flex_agent_module.FlexAgent)
    agent._create_call_runtime = lambda: runtime
    agent.workflow_backend = _BackendStub()
    agent.config = {
        "WORKSPACE": {"allow_path": [str(allow_root)]},
        "USER_ID": "config-user",
        "SESSION_ID": "session-1",
        "AGENT_CONFIG": {},
    }
    agent._pre_hooks = []
    agent._post_hooks = []
    agent.debug = False
    agent.mode = "chat"
    agent._run_builtin_agent_pre_hooks = lambda state, runtime=None: state
    agent._get_or_init_context = lambda state, runtime=None: None

    refreshed_users: list[str | None] = []

    class _ToolManagerStub:
        @staticmethod
        def refresh_user_skills(*, user_id: str | None = None):
            refreshed_users.append(user_id)

        @staticmethod
        def list_skills():
            return [{"name": "pdf", "path": str(new_skill_root)}]

        @staticmethod
        def workspace_allow_path_list(config):
            return [str(allow_root)]

    agent.env_config = SimpleNamespace(tool_manager=_ToolManagerStub())
    result = await agent.chat(
        "请执行",
        initial_state={"user_id": "runtime-user", "workspace": str(workspace)},
    )

    assert result == {"messages": [], "complete": True}
    assert refreshed_users == ["runtime-user"]
    assert captured["runtime_set"] is runtime
    assert captured["state"]["user_query"] == "请执行"
    assert captured["workspace_root"] == workspace
    assert captured["skill_aliases"] == {"pdf": new_skill_root}
    assert captured["allow_read_roots"] == [allow_root]
