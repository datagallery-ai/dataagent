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
"""Unit tests for per-tool pre/post hooks (Flex Executor)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from dataagent.actions.tools.hooks.base import (
    ToolHookInvocation,
    ToolHookRunner,
    ToolPreHookOutcome,
)
from dataagent.actions.tools.hooks.config import load_tool_hooks_from_config
from dataagent.actions.tools.local_tool.sandbox import NoopSandbox
from dataagent.core.flex.nodes.executor import Executor
from dataagent.core.managers.action_manager.base import ErrorType, ToolResult
from dataagent.core.managers.action_manager.manager import ToolManager
from dataagent.governance import attach_governance_hooks_to_tool, build_governance_config


def governance_inject_hidden(inv) -> dict[str, str]:
    inv.metadata.setdefault("events", []).append("injector")
    return {"_secret": "governed"}


def governance_inject_visible(inv) -> dict[str, str]:
    return {"value": "not allowed"}


def hidden_arg_tool(value: str, *, _secret: str = "") -> str:
    return f"{value}:{_secret}"


def _workspace_dir(tmp_path: Path) -> str:
    """Isolated workspace for Executor snapshot_dir (avoids scanning host /tmp)."""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    return str(ws.resolve())


def _make_runtime(*, call_tool, tool=None, workspace: str, governance=None, tool_manager=None):
    return SimpleNamespace(
        call_tool=call_tool,
        get_tool=lambda _name: tool,
        tool_manager=tool_manager,
        env=SimpleNamespace(governance=governance),
        sandbox=NoopSandbox(workspace_root=workspace),
        workspace_dir=workspace,
        bash_tool_whitelist=None,
        get_all_config=lambda: {},
        config_manager=None,
    )


@pytest.mark.asyncio
async def test_run_pre_hooks_shared_hook_context():
    """Multiple pre-hooks share hook_context in order."""
    seen: list[str] = []

    async def hook_a(inv: ToolHookInvocation) -> ToolPreHookOutcome:
        inv.hook_context["a"] = 1
        seen.append("a")
        return ToolPreHookOutcome()

    async def hook_b(inv: ToolHookInvocation) -> ToolPreHookOutcome:
        assert inv.hook_context.get("a") == 1
        seen.append("b")
        return ToolPreHookOutcome()

    inv = ToolHookInvocation(
        tool_name="t",
        tool_call_id="c1",
        tool_args={},
        runtime=SimpleNamespace(),
        metadata={},
    )
    await ToolHookRunner.run_pre_hooks([hook_a, hook_b], inv)
    assert seen == ["a", "b"]


@pytest.mark.asyncio
async def test_pre_hook_failure_skips_call_tool(tmp_path: Path):
    """Pre-hook failure returns error execution without invoking call_tool."""
    call_count = 0

    async def call_tool(name: str, **kwargs):
        nonlocal call_count
        call_count += 1
        return ToolResult(success=True, data="ok")

    async def failing_pre(inv: ToolHookInvocation) -> ToolPreHookOutcome:
        raise ValueError("blocked by pre-hook")

    workspace = _workspace_dir(tmp_path)
    tool = SimpleNamespace(pre_hooks=[failing_pre], post_hooks=[])
    runtime = _make_runtime(call_tool=call_tool, tool=tool, workspace=workspace)
    executor = Executor("executor")

    execution = await executor._execute_tool_call_impl(
        tool_call={"name": "my_tool", "args": {}, "id": "tc1"},
        workspace=workspace,
        user_id=None,
        session_id=None,
        sub_id=None,
        runtime=runtime,
    )

    assert call_count == 0
    assert execution.success is False
    assert "pre-hook" in execution.error_text
    assert execution.error_type == ErrorType.VALIDATION_ERROR.value
    assert execution.retry_info.get("retriable") is False


@pytest.mark.asyncio
async def test_post_hook_runs_after_tool_failure(tmp_path: Path):
    """Strategy A: post-hook runs when tool fails; hook sees success=False execution."""
    post_ran = asyncio.Event()
    seen_success: list[bool | None] = []

    async def call_tool(name: str, **kwargs):
        raise RuntimeError("tool boom")

    async def post_hook(inv: ToolHookInvocation) -> ToolPreHookOutcome:
        seen_success.append(inv.execution.success if inv.execution else None)
        post_ran.set()
        return ToolPreHookOutcome()

    workspace = _workspace_dir(tmp_path)
    tool = SimpleNamespace(pre_hooks=[], post_hooks=[post_hook])
    runtime = _make_runtime(call_tool=call_tool, tool=tool, workspace=workspace)
    executor = Executor("executor")

    execution = await executor._execute_tool_call_impl(
        tool_call={"name": "my_tool", "args": {}, "id": "tc2"},
        workspace=workspace,
        user_id=None,
        session_id=None,
        sub_id=None,
        runtime=runtime,
    )

    await asyncio.wait_for(post_ran.wait(), timeout=2.0)
    assert execution.success is False
    assert seen_success == [False]


@pytest.mark.asyncio
async def test_post_hook_failure_marks_execution_failed(tmp_path: Path):
    """Post-hook failure after successful tool marks execution as failed."""

    async def call_tool(name: str, **kwargs):
        return ToolResult(success=True, data="ok")

    async def failing_post(inv: ToolHookInvocation) -> ToolPreHookOutcome:
        raise ValueError("post failed")

    workspace = _workspace_dir(tmp_path)
    tool = SimpleNamespace(pre_hooks=[], post_hooks=[failing_post])
    runtime = _make_runtime(call_tool=call_tool, tool=tool, workspace=workspace)
    executor = Executor("executor")

    execution = await executor._execute_tool_call_impl(
        tool_call={"name": "my_tool", "args": {}, "id": "tc3"},
        workspace=workspace,
        user_id=None,
        session_id=None,
        sub_id=None,
        runtime=runtime,
    )

    assert execution.success is False
    assert "post-hook" in execution.error_text


@pytest.mark.asyncio
async def test_executor_aprocess_builds_error_tool_message_on_pre_hook_failure(tmp_path: Path):
    """End-to-end through _aprocess: pre-hook failure yields error ToolMessage."""

    async def call_tool(name: str, **kwargs):
        return ToolResult(success=True, data="ok")

    async def failing_pre(inv: ToolHookInvocation) -> ToolPreHookOutcome:
        raise ValueError("pre block")

    workspace = _workspace_dir(tmp_path)
    tool = SimpleNamespace(pre_hooks=[failing_pre], post_hooks=[])
    runtime = _make_runtime(call_tool=call_tool, tool=tool, workspace=workspace)
    executor = Executor("executor")

    state = {
        "workspace": workspace,
        "messages": [
            AIMessage(content="", tool_calls=[{"id": "tc4", "name": "my_tool", "args": {}}]),
        ],
    }
    result = await executor._aprocess(state, runtime)
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].status == "error"
    assert "pre block" in str(tool_msgs[0].content)


def test_load_tool_hooks_from_config_empty():
    """Missing or empty hooks config yields empty lists."""
    lists = load_tool_hooks_from_config(None)
    assert lists.pre == [] and lists.post == []


def test_load_tool_hooks_from_config_requires_dot_path():
    """Colon-separated specs are not supported; invalid entries are skipped."""
    lists = load_tool_hooks_from_config({"pre": ["dataagent.actions.tools.hooks.examples.example_hooks:audit_pre"]})
    assert lists.pre == [] and lists.post == []


def test_load_tool_hooks_from_config_rejects_disallowed_module():
    """Disallowed dotted paths are skipped before arbitrary module import."""
    lists = load_tool_hooks_from_config({"pre": ["os.system"]})
    assert lists.pre == [] and lists.post == []


@pytest.mark.asyncio
async def test_pre_hook_can_mutate_tool_args(tmp_path: Path):
    """Pre-hooks may add internal args before tool invocation."""
    seen: dict[str, object] = {}

    async def call_tool(name: str, **kwargs):
        seen.update(kwargs)
        return ToolResult(success=True, data="ok")

    async def inject_pre(inv: ToolHookInvocation) -> ToolPreHookOutcome:
        inv.tool_args["_secret"] = "from-pre-hook"
        return ToolPreHookOutcome()

    workspace = _workspace_dir(tmp_path)
    tool = SimpleNamespace(pre_hooks=[inject_pre], post_hooks=[])
    runtime = _make_runtime(call_tool=call_tool, tool=tool, workspace=workspace)
    executor = Executor("executor")

    execution = await executor._execute_tool_call_impl(
        tool_call={"name": "my_tool", "args": {"value": "visible"}, "id": "tc-mutable-pre"},
        workspace=workspace,
        user_id=None,
        session_id=None,
        sub_id=None,
        runtime=runtime,
    )

    assert execution.success is True
    assert seen == {"value": "visible", "_secret": "from-pre-hook"}
    assert execution.tool_args == {"value": "visible", "_secret": "from-pre-hook"}


def test_runtime_get_tools_for_llm_filters_governance_invisible_tools():
    """Invisible tools are hidden from LLM binding but remain registered."""
    tm = ToolManager()
    tm.register_local_tool(lambda: "hidden", name="submit_subagent")
    tm.register_local_tool(lambda: "visible", name="advance_data_analysis_workflow")
    governance = build_governance_config({"invisibility": ["submit_subagent"]})
    runtime = SimpleNamespace(tool_manager=tm, governance=governance)

    from dataagent.core.cbb.runtime import Runtime

    runtime_obj = Runtime(runtime)
    assert [tool.name for tool in runtime_obj.get_tools_for_llm()] == ["advance_data_analysis_workflow"]
    assert runtime_obj.get_tool("submit_subagent").name == "submit_subagent"


@pytest.mark.asyncio
async def test_tool_manager_loads_governance_hooks_from_config(tmp_path: Path):
    """ToolManager distributes GOVERNANCE rules to tools by applies_to."""
    workspace = _workspace_dir(tmp_path)
    tm = ToolManager()
    tm.init_from_config(
        {
            "TOOLS": {
                "local_functions": [
                    {
                        "module": "tests.ut.tools.test_tool_hooks",
                        "function": "hidden_arg_tool",
                    }
                ]
            },
            "GOVERNANCE": {
                "argument_injectors": [
                    {
                        "id": "inject",
                        "applies_to": ["hidden_arg_tool"],
                        "address": "tests.ut.tools.test_tool_hooks.governance_inject_hidden",
                    }
                ]
            },
        }
    )
    runtime = _make_runtime(
        call_tool=lambda name, **kwargs: tm.acall(name, **kwargs),
        tool=tm.get("hidden_arg_tool"),
        workspace=workspace,
        tool_manager=tm,
    )
    executor = Executor("executor")

    execution = await executor._execute_tool_call_impl(
        tool_call={"name": "hidden_arg_tool", "args": {"value": "visible"}, "id": "tc-loader-inject"},
        workspace=workspace,
        user_id=None,
        session_id=None,
        sub_id=None,
        runtime=runtime,
    )

    assert execution.success is True
    assert execution.raw_result == "visible:governed"


@pytest.mark.asyncio
async def test_governance_injector_rejects_visible_args(tmp_path: Path):
    """Injector may only add underscore-prefixed internal args."""

    async def call_tool(name: str, **kwargs):
        return ToolResult(success=True, data="ok")

    workspace = _workspace_dir(tmp_path)
    governance = build_governance_config(
        {"argument_injectors": [{"id": "bad", "applies_to": ["my_tool"], "address": governance_inject_visible}]}
    )
    tool = SimpleNamespace(pre_hooks=[], post_hooks=[])
    attach_governance_hooks_to_tool(governance, tool, "my_tool")
    runtime = _make_runtime(call_tool=call_tool, tool=tool, workspace=workspace)
    executor = Executor("executor")

    execution = await executor._execute_tool_call_impl(
        tool_call={"name": "my_tool", "args": {}, "id": "tc-bad-inject"},
        workspace=workspace,
        user_id=None,
        session_id=None,
        sub_id=None,
        runtime=runtime,
    )

    assert execution.success is False
    assert "underscore-prefixed" in execution.error_text
