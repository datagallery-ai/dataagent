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

from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage

from dataagent.core.context.context import ContextFactory
from dataagent.core.flex.workflow.router import FlexRouter
from dataagent.utils.converter.result_ir_converter import ResultIRConverter


@pytest.fixture(autouse=True)
def _clear_context_factory() -> None:
    ContextFactory.clear_context()
    yield
    ContextFactory.clear_context()


def test_router_write_message_history_uses_custom_session_memory_dir(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = {"WORKSPACE_POLICY": {"layout": {"session_memory_dir": ".xxmemory"}}}
    router = FlexRouter(actor_nodes=["planner", "executor"])
    router.set_merged_config(config)

    with patch(
        "dataagent.core.framework_adapters.runtime.context.get_current_runtime",
        return_value=None,
    ):
        router._write_message_history(
            {
                "user_id": "u1",
                "session_id": "s1",
                "workspace": workspace,
                "messages": [HumanMessage(content="hi")],
            }
        )

    assert (workspace / ".xxmemory" / "messages.json").is_file()
    assert not (workspace / ".memory" / "messages.json").exists()


def test_ir_converter_file_fallback_uses_custom_tool_outputs_dir(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    context = ContextFactory.get_context(
        user_id="u1",
        session_id="s1",
        run_id=0,
        sub_id=0,
    )
    context.state.config = {"WORKSPACE_POLICY": {"layout": {"tool_outputs_dir": ".xxtool_outputs/custom/"}}}
    context.register_query(query="test", additional_files=[])
    context.register_node(
        node_type="Action",
        label="act001",
        description="",
        predecessor_node=["Query(query00000)"],
        action="bash",
        params={},
        output="Pending",
        success=False,
    )

    created = ResultIRConverter._create_file_fallback(
        context,
        "x" * 600,
        "Action(act001)",
        "bash",
        workspace,
        knowledge_min_length=500,
    )

    output_dir = workspace / ".xxtool_outputs" / "custom"
    assert output_dir.is_dir()
    assert any(output_dir.iterdir())
    assert not (workspace / ".dataagent").exists()
    assert created


def test_session_history_restore_does_not_create_default_memory_dir(tmp_path) -> None:
    workspace = tmp_path / "ws"
    custom_mem = workspace / ".xxmemory"
    custom_mem.mkdir(parents=True)
    (custom_mem / "messages.json").write_text(
        '{"messages": [{"type": "HumanMessage", "content": "hi", "name": "", "additional_kwargs": {}, "response_metadata": {}}]}',
        encoding="utf-8",
    )
    config = {"WORKSPACE_POLICY": {"layout": {"session_memory_dir": ".xxmemory"}}}
    runtime = type("Runtime", (), {"get_all_config": staticmethod(lambda: config)})()

    from dataagent.core.flex.hooks.agent_turn import session_history_restore

    state = session_history_restore(
        {
            "user_id": "u1",
            "session_id": "s1",
            "workspace": workspace,
            "messages": [],
        },
        runtime,
    )

    assert len(state["messages"]) == 1
    assert not (workspace / ".memory").exists()


def test_save_messages_full_uses_custom_session_memory_dir(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = {"WORKSPACE_POLICY": {"layout": {"session_memory_dir": ".xxmemory"}}}

    from dataagent.core.flex.hooks.history_writer import save_messages_full

    save_messages_full(
        "u1",
        "s1",
        [HumanMessage(content="audit line")],
        workspace=workspace,
        config=config,
    )

    assert (workspace / ".xxmemory" / "messages_full.json").is_file()
    assert not (workspace / ".memory").exists()


def test_build_memory_str_loads_snapshot_from_custom_workspace(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "ws"
    mem_dir = workspace / ".xxmemory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "snapshot.json").write_text(
        '{"session_summary": "graded students"}',
        encoding="utf-8",
    )
    config = {"WORKSPACE_POLICY": {"layout": {"session_memory_dir": ".xxmemory"}}}
    runtime = type("Runtime", (), {"get_all_config": staticmethod(lambda: config)})()
    state = {
        "user_id": "anonymous",
        "session_id": "s1",
        "workspace": workspace,
    }

    from dataagent.core.flex.nodes.planner import _build_memory_str

    result = _build_memory_str(state, runtime=runtime)
    assert "graded students" in result
    assert not (tmp_path / "home" / "anonymous" / "s1").exists()


def test_resolve_history_persistence_context_reads_runtime_config() -> None:
    from dataagent.core.flex.hooks.history_writer import resolve_history_persistence_context

    config = {"WORKSPACE_POLICY": {"layout": {"session_memory_dir": ".xxmemory"}}}
    runtime = type("Runtime", (), {"get_all_config": staticmethod(lambda: config)})()
    workspace, merged = resolve_history_persistence_context(
        {"workspace": "/tmp/ws"},
        runtime,
    )
    assert str(workspace) == "/tmp/ws"
    assert merged == config
