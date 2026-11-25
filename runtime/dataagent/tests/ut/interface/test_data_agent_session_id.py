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
"""Unit tests for DataAgent SDK session identity handling."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import dataagent.interface.sdk.agent as sdk_agent_module
from dataagent.interface.sdk.agent import DataAgent


class _CaptureChatAgent:
    def __init__(self) -> None:
        self.session_ids: list[str] = []

    async def chat(
        self, user_query: str, *, session_id: str, initial_state: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        self.session_ids.append(session_id)
        return {"user_query": user_query, "session_id": session_id, "initial_state": initial_state}


class _MinimalConfig:
    def copy(self) -> _MinimalConfig:
        return self

    def get(self, _key: str, default: Any = None) -> Any:
        return default

    def get_all(self) -> dict[str, Any]:
        return {}


def _build_sdk_probe_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[DataAgent, _CaptureChatAgent]:
    monkeypatch.setattr(DataAgent, "global_init", lambda _self, _config: None)

    def fake_acquire_workspace_lock(**_kwargs: Any) -> sdk_agent_module.WorkspaceLockHandle:
        """Stub lock acquire so session-id tests stay decoupled from lock uuid usage."""
        return sdk_agent_module.WorkspaceLockHandle(
            workspace_root=tmp_path,
            lock_dir=tmp_path / ".lock",
            token="test-lock-token",
            owner_kind="main_session",
            owner_id="probe",
            purpose="flex_chat",
            expires_at="2099-01-01T00:00:00+00:00",
        )

    # Keep this test focused on session identity; do not couple to workspace-lock uuid usage.
    monkeypatch.setattr(sdk_agent_module, "acquire_workspace_lock", fake_acquire_workspace_lock)
    monkeypatch.setattr(sdk_agent_module, "release_workspace_lock", lambda _handle: None)
    agent = DataAgent(_MinimalConfig())
    chat_agent = _CaptureChatAgent()
    agent._chat_agent_instance = chat_agent

    def initialize_state(
        initial_state: dict[str, Any] | None = None,
        session_id: str | None = None,
        workspace: Path | str | None = None,
    ) -> dict[str, Any]:
        state = dict(initial_state or {})
        state.setdefault("workspace", tmp_path)
        state.setdefault("user_id", "ut-user")
        state["session_id"] = session_id
        return state

    agent._initialize_state = initialize_state
    agent._ensure_workspace = lambda _state: None
    agent._touch_workspace_catalog = lambda _state: None
    agent._dump_runtime_config = lambda _state: None
    monkeypatch.setattr(sdk_agent_module, "setup_session_log", lambda **_kwargs: None)
    return agent, chat_agent


def test_chat_without_session_id_generates_new_id_per_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Implicit session IDs must not be cached on the DataAgent SDK instance."""
    uuids = iter(["uuid-one", "uuid-two"])
    monkeypatch.setattr(sdk_agent_module.uuid, "uuid4", lambda: next(uuids))
    agent, chat_agent = _build_sdk_probe_agent(monkeypatch, tmp_path)

    first = asyncio.run(agent.chat("hello"))
    second = asyncio.run(agent.chat("again"))

    assert first["session_id"].endswith("uuid-one")
    assert second["session_id"].endswith("uuid-two")
    assert chat_agent.session_ids == [first["session_id"], second["session_id"]]
    assert first["session_id"] != second["session_id"]
    assert not hasattr(agent, "session_id")


def test_chat_uses_initial_state_session_id_when_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Existing callers can keep multi-turn continuity by passing initial_state.session_id."""
    agent, chat_agent = _build_sdk_probe_agent(monkeypatch, tmp_path)

    result = asyncio.run(agent.chat("hello", initial_state={"session_id": "existing-session"}))

    assert result["session_id"] == "existing-session"
    assert chat_agent.session_ids == ["existing-session"]


class TestHitlBudgetInjection:
    """AGENT_CONFIG.hitl_budget → state 注入（HITL 交互预算可配置化）。

    消费方（hitl_auto_resolver 的 get_hitl_budget / planner 预算提示）只读
    state["hitl_budget"]，无 runtime 通道——SDK chat 起始状态是唯一注入点。
    未配置时不写键，框架缺省（hitl_common.DEFAULT_HITL_BUDGET=4）不变。
    注意：注入值还须在 FlexState 声明 hitl_budget 字段才能穿越 LangGraph
    输入过滤到达图内消费方（回归测试见
    framework_adapters/test_workflow_runtime_isolation.py，曾因
    缺该声明全程 budget=4）。
    """

    def _build_agent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        agent_config: dict[str, Any] | None,
    ) -> DataAgent:
        class _Config:
            def copy(self) -> _Config:
                return self

            def get(self, key: str, default: Any = None) -> Any:
                if key == "AGENT_CONFIG":
                    return agent_config
                return default

            def get_all(self) -> dict[str, Any]:
                return {"AGENT_CONFIG": agent_config or {}}

        monkeypatch.setattr(DataAgent, "global_init", lambda _self, _config: None)
        monkeypatch.setattr(
            sdk_agent_module,
            "resolve_effective_workspace_root",
            lambda **_kwargs: tmp_path,
        )
        return DataAgent(_Config())

    def test_agent_config_hitl_budget_seeded_into_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        agent = self._build_agent(monkeypatch, tmp_path, {"hitl_budget": 8})
        state = agent._initialize_state({"user_query": "q"})
        assert state["hitl_budget"] == 8
        # 消费方同源读取
        from dataagent.core.flex.hooks.hitl_common import get_hitl_budget

        assert get_hitl_budget(state) == 8

    def test_no_hitl_budget_config_leaves_key_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        agent = self._build_agent(monkeypatch, tmp_path, {})
        state = agent._initialize_state({"user_query": "q"})
        assert "hitl_budget" not in state

    def test_caller_provided_budget_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        agent = self._build_agent(monkeypatch, tmp_path, {"hitl_budget": 8})
        state = agent._initialize_state({"user_query": "q", "hitl_budget": 3})
        assert state["hitl_budget"] == 3

    def test_invalid_budget_value_still_seeded_consumer_normalizes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """非 int 配置原样入 state，消费方 get_hitl_budget 的 int 解析兜底。"""
        agent = self._build_agent(monkeypatch, tmp_path, {"hitl_budget": "8"})
        state = agent._initialize_state({"user_query": "q"})
        assert state["hitl_budget"] == "8"
        from dataagent.core.flex.hooks.hitl_common import get_hitl_budget

        assert get_hitl_budget(state) == 8
