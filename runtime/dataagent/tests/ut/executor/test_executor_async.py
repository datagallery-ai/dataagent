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
import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from dataagent.actions.tools.local_tool.sandbox import NoopSandbox
from dataagent.core.context.context import ContextFactory
from dataagent.core.flex.nodes import executor as executor_module
from dataagent.core.flex.nodes.executor import Executor
from dataagent.core.managers.action_manager.base import ToolResult
from dataagent.utils.messages_utils import MAX_TOOL_RESULT_LENGTH


def _make_runtime(*, call_tool, workspace, tool_manager=None, bash_tool_whitelist=None, agent_config=None):
    """Build a minimal runtime with a call_tool async callable.

    Args:
        call_tool: Async callable invoked for each tool.
        workspace: Isolated directory for sandbox and IR workspace snapshots (use pytest ``tmp_path``).
        tool_manager: Optional tool manager for schema lookup.
        bash_tool_whitelist: Optional bash command whitelist.
        agent_config: Per-Agent config dict for swarm helper tests.
    """
    ws = str(Path(workspace).resolve())
    cfg = dict(agent_config or {})

    def get_all_config():
        import copy

        return copy.deepcopy(cfg)

    return SimpleNamespace(
        call_tool=call_tool,
        tool_manager=tool_manager,
        bash_tool_whitelist=bash_tool_whitelist,
        sandbox=NoopSandbox(workspace_root=ws),
        workspace_dir=ws,
        get_all_config=get_all_config,
        config_manager=None,
    )


def _wrap_tools_as_call_tool(tools: dict):
    """Return an async call_tool that dispatches sync/async tools correctly (same threading
    behaviour as LocalToolWrapper.acall)."""

    async def call_tool(name: str, **kwargs):
        func = tools[name]
        if asyncio.iscoroutinefunction(func):
            result = await func(**kwargs)
        else:
            result = await asyncio.to_thread(func, **kwargs)
        if isinstance(result, ToolResult):
            return result
        return ToolResult(success=True, data=result)

    return call_tool


@pytest.fixture(autouse=True)
def clear_context():
    ContextFactory.clear_context()
    yield
    ContextFactory.clear_context()


def _build_state(message: AIMessage, workspace: str | None = None) -> dict:
    return {
        "messages": [message],
        "user_id": "test-user",
        "session_id": "test-session",
        "run_id": 0,
        "sub_id": 0,
        "workspace": workspace,
    }


def _tool_messages_only(result) -> list[ToolMessage]:
    """BaseNode.aprocess prepends state.messages (AIMessage) to result.messages;
    filter to only ToolMessages so tests assert on what the executor actually produced."""
    return [m for m in result["messages"] if isinstance(m, ToolMessage)]


def test_normalize_payload_serializes_structured_messages_as_json():
    executor = Executor("executor")

    execution = executor._normalize_payload(
        tool_name="sub_agent_tool",
        tool_call_id="call-1",
        tool_args={},
        raw_result={
            "original_msg": {"status": "success", "summary": "完成"},
            "frontend_msg": ["ui", "完成"],
        },
        metadata={},
    )

    assert execution.original_msg == json.dumps({"status": "success", "summary": "完成"}, ensure_ascii=False)
    assert execution.frontend_msg == json.dumps(["ui", "完成"], ensure_ascii=False)
    assert execution.output_text == execution.original_msg


@pytest.mark.asyncio
async def test_executor_aprocess_adapts_env_sync_and_async_tools(monkeypatch, tmp_path):
    recorded_messages = []
    convert_calls = []
    main_thread_id = threading.get_ident()
    seen_threads = {}

    def fake_record_message(_context, message):
        recorded_messages.append(message.tool_call_id)

    def fake_convert(**kwargs):
        convert_calls.append(kwargs["result"])
        return []

    monkeypatch.setattr(executor_module, "record_message", fake_record_message)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(fake_convert))

    def sync_tool(value: int):
        seen_threads["sync"] = threading.get_ident()
        return {"original_msg": f"sync:{value}", "frontend_msg": "sync frontend"}

    async def async_tool(value: int):
        seen_threads["async"] = threading.get_ident()
        return {"original_msg": f"async:{value}", "frontend_msg": "async frontend"}

    tools = {"sync_tool": sync_tool, "async_tool": async_tool}
    ws = str(tmp_path)
    runtime = _make_runtime(call_tool=_wrap_tools_as_call_tool(tools), workspace=ws)

    executor = Executor("executor")
    message = AIMessage(
        content="",
        tool_calls=[
            {"id": "sync-call", "name": "sync_tool", "args": {"value": 1}},
            {"id": "async-call", "name": "async_tool", "args": {"value": 2}},
        ],
        invalid_tool_calls=[],
    )

    state_updates = await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)

    assert seen_threads["sync"] != main_thread_id
    assert seen_threads["async"] == main_thread_id
    assert [tool_message.content for tool_message in _tool_messages_only(state_updates)] == ["sync:1", "async:2"]
    assert recorded_messages == ["sync-call", "async-call"]
    # _convert_ir fires in asyncio.wait completion order (async tool finishes first)
    assert len(convert_calls) == 2
    assert {"original_msg": "sync:1", "frontend_msg": "sync frontend"} in convert_calls
    assert {"original_msg": "async:2", "frontend_msg": "async frontend"} in convert_calls


@pytest.mark.asyncio
async def test_executor_aprocess_uses_runtime_call_tool(monkeypatch, tmp_path):
    recorded_messages = []
    convert_calls = []

    def fake_record_message(_context, message):
        recorded_messages.append((message.tool_call_id, message.status))

    def fake_convert(**kwargs):
        convert_calls.append(kwargs["result"])
        return []

    async def fake_call_tool(name: str, **kwargs):
        if name == "ok_tool":
            return ToolResult(
                success=True,
                data={"original_msg": "ok body", "frontend_msg": "ok ui", "data": {"value": kwargs["value"]}},
                metadata={"source": "fake"},
            )
        if name == "bad_tool":
            return ToolResult(success=False, error="boom")
        raise RuntimeError("unexpected tool")

    monkeypatch.setattr(executor_module, "record_message", fake_record_message)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(fake_convert))

    ws = str(tmp_path)
    runtime = SimpleNamespace(
        call_tool=fake_call_tool,
        tool_manager=None,
        bash_tool_whitelist=None,
        sandbox=NoopSandbox(workspace_root=ws),
        workspace_dir=ws,
        config_manager=None,
    )

    executor = Executor("executor")
    message = AIMessage(
        content="",
        tool_calls=[
            {"id": "ok-call", "name": "ok_tool", "args": {"value": 3}},
            {"id": "bad-call", "name": "bad_tool", "args": {}},
        ],
        invalid_tool_calls=[],
    )

    state_updates = await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)

    assert state_updates["messages"][1].content == "ok body"
    assert state_updates["messages"][1].status == "success"
    assert "Error executing bad_tool: boom" in str(state_updates["messages"][2].content)
    assert state_updates["messages"][2].status == "error"
    assert recorded_messages == [("ok-call", "success"), ("bad-call", "error")]
    assert convert_calls == [{"value": 3}]


@pytest.mark.asyncio
async def test_executor_aprocess_uses_runtime_for_non_env_tool(monkeypatch, tmp_path):
    recorded_messages = []
    convert_calls = []

    def fake_record_message(_context, message):
        recorded_messages.append((message.tool_call_id, message.status))

    def fake_convert(**kwargs):
        convert_calls.append(kwargs["result"])
        return []

    async def env_tool():
        return {"original_msg": "env body", "frontend_msg": "env ui"}

    async def runtime_tool(value: int):
        return {"original_msg": f"rt:{value}", "frontend_msg": "rt ui", "data": {"value": value}}

    monkeypatch.setattr(executor_module, "record_message", fake_record_message)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(fake_convert))

    tools = {"env_tool": env_tool, "runtime_tool": runtime_tool}
    ws = str(tmp_path)
    runtime = _make_runtime(call_tool=_wrap_tools_as_call_tool(tools), workspace=ws)

    executor = Executor("executor")
    message = AIMessage(
        content="",
        tool_calls=[
            {"id": "env-call", "name": "env_tool", "args": {}},
            {"id": "rt-call", "name": "runtime_tool", "args": {"value": 9}},
        ],
        invalid_tool_calls=[],
    )

    state_updates = await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)

    assert [tool_message.content for tool_message in _tool_messages_only(state_updates)] == ["env body", "rt:9"]
    assert recorded_messages == [("env-call", "success"), ("rt-call", "success")]
    # _convert_ir fires in asyncio.wait completion order
    assert len(convert_calls) == 2
    assert {"original_msg": "env body", "frontend_msg": "env ui"} in convert_calls
    assert {"value": 9} in convert_calls


@pytest.mark.asyncio
async def test_executor_aprocess_sync_tool_runs_in_thread(monkeypatch, tmp_path):
    """Sync tools are dispatched via asyncio.to_thread, so they run in a different thread."""
    seen_threads = {}
    main_thread_id = threading.get_ident()

    def fake_record_message(_context, message):
        return None

    monkeypatch.setattr(executor_module, "record_message", fake_record_message)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(lambda **kwargs: []))

    def sync_env_tool():
        seen_threads["worker"] = threading.get_ident()
        return {"original_msg": "sync result", "frontend_msg": "sync ui"}

    tools = {"sync_env_tool": sync_env_tool}
    ws = str(tmp_path)
    runtime = _make_runtime(call_tool=_wrap_tools_as_call_tool(tools), workspace=ws)

    executor = Executor("executor")
    message = AIMessage(
        content="",
        tool_calls=[{"id": "sync-call", "name": "sync_env_tool", "args": {}}],
        invalid_tool_calls=[],
    )

    state_updates = await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)

    assert seen_threads["worker"] != main_thread_id
    assert [tool_message.content for tool_message in _tool_messages_only(state_updates)] == ["sync result"]


@pytest.mark.asyncio
async def test_executor_aprocess_preserves_original_order_for_parallel_tools(monkeypatch, tmp_path):
    recorded_messages = []
    convert_calls = []

    def fake_record_message(_context, message):
        recorded_messages.append(message.tool_call_id)

    def fake_convert(**kwargs):
        convert_calls.append(kwargs["tool_call_id"])
        return []

    monkeypatch.setattr(executor_module, "record_message", fake_record_message)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(fake_convert))

    async def slow_bash():
        await asyncio.sleep(0.05)
        return {"original_msg": "bash result", "frontend_msg": "bash ui"}

    async def fast_subagent():
        await asyncio.sleep(0.01)
        return {"original_msg": "subagent result", "frontend_msg": "subagent ui"}

    tools = {"bash": slow_bash, "sub_agent_tool": fast_subagent}
    ws = str(tmp_path)
    runtime = _make_runtime(call_tool=_wrap_tools_as_call_tool(tools), workspace=ws)

    executor = Executor("executor")
    message = AIMessage(
        content="",
        tool_calls=[
            {"id": "bash-call", "name": "bash", "args": {}},
            {"id": "subagent-call", "name": "sub_agent_tool", "args": {}},
        ],
        invalid_tool_calls=[],
    )

    state_updates = await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)

    # ToolMessages are in original insertion order
    assert [tool_message.tool_call_id for tool_message in _tool_messages_only(state_updates)] == [
        "bash-call",
        "subagent-call",
    ]
    # record_message is also called in insertion order (line 152 loop)
    assert recorded_messages == ["bash-call", "subagent-call"]
    # But _convert_ir fires inside asyncio.wait loop (completion order)
    assert set(convert_calls) == {"bash-call", "subagent-call"}


@pytest.mark.asyncio
async def test_executor_rejects_duplicate_explicit_subagent_sub_id(monkeypatch, tmp_path):
    recorded_messages = []
    calls: list[dict] = []

    def fake_record_message(_context, message):
        recorded_messages.append((message.tool_call_id, message.status, str(message.content)))

    monkeypatch.setattr(executor_module, "record_message", fake_record_message)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(lambda **kwargs: []))

    async def subagent_tool(**kwargs):
        calls.append(kwargs)
        return {"original_msg": "should not happen", "frontend_msg": "should not happen"}

    tools = {"sub_agent_tool": subagent_tool}
    ws = str(tmp_path)
    runtime = _make_runtime(call_tool=_wrap_tools_as_call_tool(tools), workspace=ws)

    executor = Executor("executor")
    message = AIMessage(
        content="",
        tool_calls=[
            {"id": "subagent-1", "name": "sub_agent_tool", "args": {"query": "a", "sub_id": 123456}},
            {"id": "subagent-2", "name": "sub_agent_tool", "args": {"query": "b", "sub_id": 123456}},
        ],
        invalid_tool_calls=[],
    )

    state_updates = await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)

    tool_messages = _tool_messages_only(state_updates)
    assert calls == []
    assert [msg.status for msg in tool_messages] == ["error", "error"]
    assert all("duplicate sub_id" in str(msg.content) for msg in tool_messages)
    assert recorded_messages == [
        ("subagent-1", "error", str(tool_messages[0].content)),
        ("subagent-2", "error", str(tool_messages[1].content)),
    ]


@pytest.mark.asyncio
async def test_executor_enforces_swarm_worker_max_concurrent(monkeypatch, tmp_path):
    recorded_messages = []
    calls: list[str] = []

    def fake_record_message(_context, message):
        recorded_messages.append((message.tool_call_id, message.status))

    monkeypatch.setattr(executor_module, "record_message", fake_record_message)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(lambda **kwargs: []))

    async def subagent_tool(**kwargs):
        calls.append(str(kwargs.get("query") or ""))
        return {"original_msg": "ok", "frontend_msg": "ok"}

    tools = {"sub_agent_tool": subagent_tool}
    ws = str(tmp_path)
    runtime = _make_runtime(
        call_tool=_wrap_tools_as_call_tool(tools),
        workspace=ws,
        agent_config={"SWARM": {"enable": True, "worker_max_concurrent": 2}},
    )

    executor = Executor("executor")
    message = AIMessage(
        content="",
        tool_calls=[
            {"id": "subagent-1", "name": "sub_agent_tool", "args": {"query": "a", "sub_id": 111111}},
            {"id": "subagent-2", "name": "sub_agent_tool", "args": {"query": "b", "sub_id": 222222}},
            {"id": "subagent-3", "name": "sub_agent_tool", "args": {"query": "c", "sub_id": 333333}},
        ],
        invalid_tool_calls=[],
    )

    state_updates = await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)

    tool_messages = _tool_messages_only(state_updates)
    assert len(tool_messages) == 3
    assert calls == ["a", "b"]
    assert [msg.status for msg in tool_messages] == ["success", "success", "error"]
    assert "parallel sub_agent_tool limit exceeded" in str(tool_messages[2].content)


@pytest.mark.asyncio
async def test_executor_skips_swarm_subagent_cap_when_max_concurrent_none(monkeypatch, tmp_path):
    """When ``swarm_worker_max_concurrent()`` is unset (None), do not block extra sub_agent_tool calls."""
    recorded_messages = []
    calls: list[str] = []

    def fake_record_message(_context, message):
        recorded_messages.append((message.tool_call_id, message.status))

    monkeypatch.setattr(executor_module, "record_message", fake_record_message)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(lambda **kwargs: []))

    async def subagent_tool(**kwargs):
        calls.append(str(kwargs.get("query") or ""))
        return {"original_msg": "ok", "frontend_msg": "ok"}

    tools = {"sub_agent_tool": subagent_tool}
    ws = str(tmp_path)
    runtime = _make_runtime(
        call_tool=_wrap_tools_as_call_tool(tools),
        workspace=ws,
        agent_config={"SWARM": {"enable": True}},
    )

    executor = Executor("executor")
    message = AIMessage(
        content="",
        tool_calls=[
            {"id": "subagent-1", "name": "sub_agent_tool", "args": {"query": "a", "sub_id": 111111}},
            {"id": "subagent-2", "name": "sub_agent_tool", "args": {"query": "b", "sub_id": 222222}},
            {"id": "subagent-3", "name": "sub_agent_tool", "args": {"query": "c", "sub_id": 333333}},
        ],
        invalid_tool_calls=[],
    )

    state_updates = await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)

    tool_messages = _tool_messages_only(state_updates)
    assert len(tool_messages) == 3
    assert calls == ["a", "b", "c"]
    assert all(msg.status == "success" for msg in tool_messages)


@pytest.mark.asyncio
async def test_executor_invalid_tool_calls_stay_before_valid_results(monkeypatch, tmp_path):
    recorded_messages = []

    def fake_record_message(_context, message):
        recorded_messages.append(message.tool_call_id)

    monkeypatch.setattr(executor_module, "record_message", fake_record_message)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(lambda **kwargs: []))

    async def valid_tool():
        return {"original_msg": "valid result", "frontend_msg": "valid ui"}

    tools = {"valid_tool": valid_tool}
    ws = str(tmp_path)
    runtime = _make_runtime(call_tool=_wrap_tools_as_call_tool(tools), workspace=ws)

    executor = Executor("executor")
    message = AIMessage(
        content="",
        tool_calls=[{"id": "valid-call", "name": "valid_tool", "args": {}}],
        invalid_tool_calls=[{"id": "invalid-call", "name": "missing_tool", "error": "bad args"}],
    )

    state_updates = await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)

    assert "process" not in Executor.__dict__
    tool_msgs = _tool_messages_only(state_updates)
    assert [tool_message.tool_call_id for tool_message in tool_msgs] == ["invalid-call", "valid-call"]
    assert tool_msgs[0].status == "error"
    assert tool_msgs[1].content == "valid result"
    assert recorded_messages == ["invalid-call", "valid-call"]


# ── ToolMessage length safeguard tests ──────────────────────────────


@pytest.mark.asyncio
async def test_executor_short_result_not_replaced(monkeypatch, tmp_path):
    """Result under MAX_TOOL_RESULT_LENGTH should pass through unchanged."""
    monkeypatch.setattr(executor_module, "record_message", lambda _ctx, _msg: None)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(lambda **kwargs: []))

    async def short_tool():
        return {"original_msg": "short result", "frontend_msg": "short ui"}

    tools = {"short_tool": short_tool}
    ws = str(tmp_path)
    runtime = _make_runtime(call_tool=_wrap_tools_as_call_tool(tools), workspace=ws)

    executor = Executor("executor")
    message = AIMessage(
        content="",
        tool_calls=[{"id": "short-call", "name": "short_tool", "args": {}}],
        invalid_tool_calls=[],
    )

    state_updates = await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)
    content = _tool_messages_only(state_updates)[0].content
    assert content == "short result"
    assert "[IR Summary]" not in content


@pytest.mark.asyncio
async def test_executor_long_result_replaced_with_ir(monkeypatch, tmp_path):
    """Result >= MAX_TOOL_RESULT_LENGTH with IR nodes should be replaced with IR summary."""
    monkeypatch.setattr(executor_module, "record_message", lambda _ctx, _msg: None)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(lambda **kwargs: []))

    def fake_try_replace(msg, _ctx):
        return ToolMessage(
            content='[IR Summary] tool=huge_tool\nArtifacts produced:\n- File(file00001) ""  \n  Original content: file stored at `/tmp/test_output.txt` | To restore: `cat /tmp/test_output.txt`',
            tool_call_id=msg.tool_call_id,
            name=msg.name,
        )

    monkeypatch.setattr(executor_module, "try_replace_with_ir", fake_try_replace)

    huge_text = "x" * (MAX_TOOL_RESULT_LENGTH + 1)

    async def huge_tool():
        return {"original_msg": huge_text, "frontend_msg": "huge ui"}

    tools = {"huge_tool": huge_tool}
    ws = str(tmp_path)
    runtime = _make_runtime(call_tool=_wrap_tools_as_call_tool(tools), workspace=ws)

    executor = Executor("executor")
    message = AIMessage(
        content="",
        tool_calls=[{"id": "huge-call", "name": "huge_tool", "args": {}}],
        invalid_tool_calls=[],
    )

    state_updates = await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)
    content = _tool_messages_only(state_updates)[0].content
    assert "[IR Summary]" in content
    assert "huge_tool" in content
    assert "cat /tmp/test_output.txt" in content


@pytest.mark.asyncio
async def test_executor_long_result_truncation_fallback(monkeypatch, tmp_path):
    """Result >= MAX_TOOL_RESULT_LENGTH without IR nodes should fall back to truncation."""
    monkeypatch.setattr(executor_module, "record_message", lambda _ctx, _msg: None)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(lambda **kwargs: []))

    huge_text = "y" * (MAX_TOOL_RESULT_LENGTH + 1)

    async def huge_tool():
        return {"original_msg": huge_text, "frontend_msg": "huge ui"}

    tools = {"huge_tool": huge_tool}
    ws = str(tmp_path)
    runtime = _make_runtime(call_tool=_wrap_tools_as_call_tool(tools), workspace=ws)

    executor = Executor("executor")
    message = AIMessage(
        content="",
        tool_calls=[{"id": "huge-call", "name": "huge_tool", "args": {}}],
        invalid_tool_calls=[],
    )

    state_updates = await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)
    content = _tool_messages_only(state_updates)[0].content
    assert "truncated" in content
    assert content.startswith("y" * MAX_TOOL_RESULT_LENGTH)


@pytest.mark.asyncio
async def test_executor_custom_max_tool_result_length(monkeypatch, tmp_path):
    """Custom max_tool_result_length from config should override the default."""
    monkeypatch.setattr(executor_module, "record_message", lambda _ctx, _msg: None)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(lambda **kwargs: []))

    # 500 chars > custom limit of 100
    medium_text = "z" * 500

    async def medium_tool():
        return {"original_msg": medium_text, "frontend_msg": "medium ui"}

    tools = {"medium_tool": medium_tool}
    ws = str(tmp_path)
    runtime = _make_runtime(call_tool=_wrap_tools_as_call_tool(tools), workspace=ws)

    executor = Executor("executor", max_tool_result_length=100)
    message = AIMessage(
        content="",
        tool_calls=[{"id": "medium-call", "name": "medium_tool", "args": {}}],
        invalid_tool_calls=[],
    )

    state_updates = await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)
    content = _tool_messages_only(state_updates)[0].content
    assert "truncated" in content
    assert content.startswith("z" * 100)


@pytest.mark.asyncio
async def test_executor_raw_result_preserved_for_ir(monkeypatch, tmp_path):
    """Truncation must not affect raw_result passed to IR converter."""
    convert_results = []

    def fake_convert(**kwargs):
        convert_results.append(kwargs["result"])
        return []

    monkeypatch.setattr(executor_module, "record_message", lambda _ctx, _msg: None)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(fake_convert))

    huge_text = "w" * MAX_TOOL_RESULT_LENGTH

    async def huge_tool():
        return {"original_msg": huge_text, "data": {"real": "payload"}}

    tools = {"huge_tool": huge_tool}
    ws = str(tmp_path)
    runtime = _make_runtime(call_tool=_wrap_tools_as_call_tool(tools), workspace=ws)

    executor = Executor("executor")
    message = AIMessage(
        content="",
        tool_calls=[{"id": "huge-call", "name": "huge_tool", "args": {}}],
        invalid_tool_calls=[],
    )

    await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)
    assert len(convert_results) == 1
    # raw_result passed to IR converter should be the unwrapped payload, not truncated
    assert convert_results[0] == {"real": "payload"}


@pytest.mark.asyncio
async def test_executor_passes_original_msg_as_ir_visible_result(monkeypatch, tmp_path):
    """IR fallback threshold should use original_msg even when data/frontend_msg are large."""
    convert_calls = []

    def fake_convert(**kwargs):
        convert_calls.append(
            {
                "result": kwargs.get("result"),
                "visible_result": kwargs.get("visible_result"),
            }
        )
        return []

    monkeypatch.setattr(executor_module, "record_message", lambda _ctx, _msg: None)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(fake_convert))

    async def tool_with_large_data():
        return {
            "original_msg": "short visible result",
            "frontend_msg": "ui " * 300,
            "data": {"payload": "data " * 300},
        }

    tools = {"tool_with_large_data": tool_with_large_data}
    ws = str(tmp_path)
    runtime = _make_runtime(call_tool=_wrap_tools_as_call_tool(tools), workspace=ws)

    executor = Executor("executor")
    message = AIMessage(
        content="",
        tool_calls=[{"id": "large-data-call", "name": "tool_with_large_data", "args": {}}],
        invalid_tool_calls=[],
    )

    await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)
    assert convert_calls == [
        {
            "result": {"payload": "data " * 300},
            "visible_result": "short visible result",
        }
    ]


@pytest.mark.asyncio
async def test_executor_visible_result_falls_back_to_output_text_without_original_msg(monkeypatch, tmp_path):
    """无 original_msg 时，visible_result 应回退到模型可见的 output_text（= frontend_msg），
    而非 data 部分——否则 data 很大仍会错误触发长文本落盘。"""
    convert_calls = []

    def fake_convert(**kwargs):
        convert_calls.append(
            {
                "result": kwargs.get("result"),
                "visible_result": kwargs.get("visible_result"),
            }
        )
        return []

    monkeypatch.setattr(executor_module, "record_message", lambda _ctx, _msg: None)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(fake_convert))

    async def tool_no_original_msg():
        return {
            "frontend_msg": "visible frontend text",
            "data": {"payload": "data " * 300},
        }

    tools = {"tool_no_original_msg": tool_no_original_msg}
    ws = str(tmp_path)
    runtime = _make_runtime(call_tool=_wrap_tools_as_call_tool(tools), workspace=ws)

    executor = Executor("executor")
    message = AIMessage(
        content="",
        tool_calls=[{"id": "no-orig-call", "name": "tool_no_original_msg", "args": {}}],
        invalid_tool_calls=[],
    )

    await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)
    assert convert_calls == [
        {
            "result": {"payload": "data " * 300},
            "visible_result": "visible frontend text",
        }
    ]


@pytest.mark.asyncio
async def test_executor_visible_result_plain_string(monkeypatch, tmp_path):
    """非标准 dict 外壳（直接返回字符串）时，visible_result 即该字符串本身。"""
    convert_calls = []

    def fake_convert(**kwargs):
        convert_calls.append(kwargs.get("visible_result"))

    monkeypatch.setattr(executor_module, "record_message", lambda _ctx, _msg: None)
    monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(fake_convert))

    async def plain_tool():
        return "plain tool output"

    tools = {"plain_tool": plain_tool}
    ws = str(tmp_path)
    runtime = _make_runtime(call_tool=_wrap_tools_as_call_tool(tools), workspace=ws)

    executor = Executor("executor")
    message = AIMessage(
        content="",
        tool_calls=[{"id": "plain-call", "name": "plain_tool", "args": {}}],
        invalid_tool_calls=[],
    )

    await executor.aprocess(_build_state(message, workspace=ws), runtime=runtime)
    assert convert_calls == ["plain tool output"]
