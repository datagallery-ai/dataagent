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
import pathlib

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from dataagent.core.flex.nodes import executor as executor_module
from dataagent.core.flex.nodes.executor import Executor, _extract_base_commands


class _StubSandbox:
    """Minimal sandbox stub for testing."""

    def __init__(self, root: str = "/tmp"):
        self.workspace_root = pathlib.Path(root)
        self.skill_aliases: dict[str, pathlib.Path] = {}


class StubRuntime:
    def __init__(self, whitelist: list[str] | None = None, workspace_dir: str | None = None):
        self._whitelist = whitelist
        self.workspace_dir = workspace_dir
        self._sandbox = _StubSandbox(workspace_dir or "/tmp")
        self.tool_manager = None
        self.config_manager = None
        self._call_tool_fn = None

    @property
    def bash_tool_whitelist(self) -> list[str] | None:
        return self._whitelist

    @property
    def sandbox(self):
        return self._sandbox

    async def call_tool(self, name: str, **kwargs):
        if self._call_tool_fn is not None:
            return await self._call_tool_fn(name, **kwargs)
        raise KeyError(f"Tool {name!r} not found in stub runtime")


class TestExtractBaseCommands:
    def test_simple_command(self):
        assert _extract_base_commands("ls -la") == ["ls"]

    def test_pipeline(self):
        assert _extract_base_commands("cat file.txt | grep foo") == ["cat", "grep"]

    def test_and_separator(self):
        assert _extract_base_commands("ls && pwd") == ["ls", "pwd"]

    def test_or_separator(self):
        assert _extract_base_commands("ls || echo fail") == ["ls", "echo"]

    def test_semicolon(self):
        assert _extract_base_commands("cd /tmp; ls") == ["cd", "ls"]

    def test_full_path_command(self):
        assert _extract_base_commands("/usr/bin/python script.py") == ["python"]

    def test_variable_assignment(self):
        assert _extract_base_commands("VAR=val ls -la") == ["ls"]

    def test_newline_separator(self):
        assert _extract_base_commands("ls\npwd") == ["ls", "pwd"]

    def test_empty_string(self):
        assert _extract_base_commands("") == []

    def test_whitespace_only(self):
        assert _extract_base_commands("   ") == []

    def test_complex_chain(self):
        cmd = "cd /tmp && ls -la | grep foo; echo done || echo failed"
        assert _extract_base_commands(cmd) == ["cd", "ls", "grep", "echo", "echo"]


class TestBashWhitelistInExecutor:
    """Test that the whitelist check in Executor._execute_tool_call works correctly."""

    @staticmethod
    def _only_tool(result: dict) -> list[ToolMessage]:
        return [m for m in result["messages"] if isinstance(m, ToolMessage)]

    @pytest.mark.asyncio
    async def test_allowed_command_passes(self, monkeypatch):
        """A command in the whitelist should execute normally."""
        monkeypatch.setattr(executor_module, "record_message", lambda _ctx, _msg: None)
        monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(lambda **kwargs: []))

        async def fake_call_tool(name: str, **kwargs):
            from dataagent.core.managers.action_manager.base import ToolResult

            assert name == "bash"
            return ToolResult(
                success=True,
                data={"original_msg": "ok", "frontend_msg": "ok", "data": {"exit_code": 0}},
            )

        runtime = StubRuntime(whitelist=["ls", "cat", "echo"])
        runtime._call_tool_fn = fake_call_tool
        executor = Executor("executor", None)
        message = AIMessage(
            content="",
            tool_calls=[{"id": "call-1", "name": "bash", "args": {"command": "ls -la", "purpose": "list files"}}],
            invalid_tool_calls=[],
        )

        state = {
            "messages": [message],
            "user_id": "test-user",
            "session_id": "test-session",
            "run_id": 0,
            "sub_id": 0,
        }

        result = await executor.aprocess(state, runtime=runtime)
        tool_msgs = [m for m in self._only_tool(result) if m.status == "success"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].content == "ok"

    @pytest.mark.asyncio
    async def test_disallowed_command_blocked(self, monkeypatch):
        """A command not in the whitelist should be blocked with a validation error."""
        monkeypatch.setattr(executor_module, "record_message", lambda _ctx, _msg: None)
        monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(lambda **kwargs: []))

        acall_count = 0

        async def fake_call_tool(name: str, **kwargs):
            nonlocal acall_count
            acall_count += 1
            from dataagent.core.managers.action_manager.base import ToolResult

            return ToolResult(success=True, data={"original_msg": "ok", "frontend_msg": "ok"})

        runtime = StubRuntime(whitelist=["ls", "echo"])
        runtime._call_tool_fn = fake_call_tool
        executor = Executor("executor", None)
        message = AIMessage(
            content="",
            tool_calls=[{"id": "call-1", "name": "bash", "args": {"command": "rm -rf /tmp", "purpose": "cleanup"}}],
            invalid_tool_calls=[],
        )

        state = {
            "messages": [message],
            "user_id": "test-user",
            "session_id": "test-session",
            "run_id": 0,
            "sub_id": 0,
        }

        result = await executor.aprocess(state, runtime=runtime)
        tool_msgs = [m for m in self._only_tool(result) if m.status == "error"]
        assert len(tool_msgs) == 1
        assert "validation" in tool_msgs[0].content.lower()
        assert "rm" in tool_msgs[0].content
        assert acall_count == 0  # tool should never be invoked

    @pytest.mark.asyncio
    async def test_no_whitelist_allows_any_command(self, monkeypatch):
        """When no whitelist is configured (None), any command should pass."""
        monkeypatch.setattr(executor_module, "record_message", lambda _ctx, _msg: None)
        monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(lambda **kwargs: []))

        async def fake_call_tool(name: str, **kwargs):
            from dataagent.core.managers.action_manager.base import ToolResult

            assert name == "bash"
            return ToolResult(
                success=True,
                data={"original_msg": "ok", "frontend_msg": "ok", "data": {"exit_code": 0}},
            )

        runtime = StubRuntime(whitelist=None)  # no restriction
        runtime._call_tool_fn = fake_call_tool
        executor = Executor("executor", None)
        message = AIMessage(
            content="",
            tool_calls=[{"id": "call-1", "name": "bash", "args": {"command": "rm -rf /tmp", "purpose": "cleanup"}}],
            invalid_tool_calls=[],
        )

        state = {
            "messages": [message],
            "user_id": "test-user",
            "session_id": "test-session",
            "run_id": 0,
            "sub_id": 0,
        }

        result = await executor.aprocess(state, runtime=runtime)
        tool_msgs = [m for m in self._only_tool(result) if m.status == "success"]
        assert len(tool_msgs) == 1

    @pytest.mark.asyncio
    async def test_pipeline_all_commands_must_be_allowed(self, monkeypatch):
        """In a pipeline, every base command must be in the whitelist."""
        monkeypatch.setattr(executor_module, "record_message", lambda _ctx, _msg: None)
        monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(lambda **kwargs: []))

        acall_count = 0

        async def fake_call_tool(name: str, **kwargs):
            nonlocal acall_count
            acall_count += 1
            from dataagent.core.managers.action_manager.base import ToolResult

            return ToolResult(success=True, data={"original_msg": "ok"})

        # cat is allowed, but grep is NOT in the whitelist
        runtime = StubRuntime(whitelist=["ls", "cat", "echo"])
        runtime._call_tool_fn = fake_call_tool
        executor = Executor("executor", None)
        message = AIMessage(
            content="",
            tool_calls=[
                {"id": "call-1", "name": "bash", "args": {"command": "cat file.txt | grep foo", "purpose": "search"}}
            ],
            invalid_tool_calls=[],
        )

        state = {
            "messages": [message],
            "user_id": "test-user",
            "session_id": "test-session",
            "run_id": 0,
            "sub_id": 0,
        }

        result = await executor.aprocess(state, runtime=runtime)
        tool_msgs = [m for m in self._only_tool(result) if m.status == "error"]
        assert len(tool_msgs) == 1
        assert "validation" in tool_msgs[0].content.lower()
        assert "grep" in tool_msgs[0].content
        assert acall_count == 0

    @pytest.mark.asyncio
    async def test_non_bash_tool_not_affected(self, monkeypatch):
        """Non-bash tools should not be checked against the whitelist."""
        monkeypatch.setattr(executor_module, "record_message", lambda _ctx, _msg: None)
        monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(lambda **kwargs: []))

        async def fake_call_tool(name: str, **kwargs):
            from dataagent.core.managers.action_manager.base import ToolResult

            return ToolResult(
                success=True,
                data={"original_msg": f"result from {name}", "frontend_msg": "ok"},
            )

        runtime = StubRuntime(whitelist=["ls"])  # restrictive whitelist
        runtime._call_tool_fn = fake_call_tool
        executor = Executor("executor", None)
        message = AIMessage(
            content="",
            tool_calls=[
                {"id": "call-1", "name": "read_file", "args": {"path": "/etc/passwd", "purpose": "test"}},
                {"id": "call-2", "name": "grep", "args": {"pattern": "foo", "path": "/tmp"}},
            ],
            invalid_tool_calls=[],
        )

        state = {
            "messages": [message],
            "user_id": "test-user",
            "session_id": "test-session",
            "run_id": 0,
            "sub_id": 0,
        }

        result = await executor.aprocess(state, runtime=runtime)
        tool_msgs = [m for m in self._only_tool(result) if m.status == "success"]
        assert len(tool_msgs) == 2

    @pytest.mark.asyncio
    async def test_whitelist_with_empty_command(self, monkeypatch):
        """An empty command should pass whitelist check (no commands to validate)."""
        monkeypatch.setattr(executor_module, "record_message", lambda _ctx, _msg: None)
        monkeypatch.setattr(executor_module.ResultIRConverter, "convert", staticmethod(lambda **kwargs: []))

        async def fake_call_tool(name: str, **kwargs):
            from dataagent.core.managers.action_manager.base import ToolResult

            return ToolResult(
                success=True,
                data={"original_msg": "ok", "frontend_msg": "ok"},
            )

        runtime = StubRuntime(whitelist=["ls"])
        runtime._call_tool_fn = fake_call_tool
        executor = Executor("executor", None)
        message = AIMessage(
            content="",
            tool_calls=[{"id": "call-1", "name": "bash", "args": {"command": "", "purpose": "empty test"}}],
            invalid_tool_calls=[],
        )

        state = {
            "messages": [message],
            "user_id": "test-user",
            "session_id": "test-session",
            "run_id": 0,
            "sub_id": 0,
        }

        result = await executor.aprocess(state, runtime=runtime)
        tool_msgs = [m for m in self._only_tool(result) if m.status == "success"]
        assert len(tool_msgs) == 1
