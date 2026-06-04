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
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from langchain_core.messages import AIMessage, HumanMessage

from dataagent.actions.tools.local_tool.sandbox import (
    NoopSandbox,
    reset_current_sandbox,
    set_current_sandbox,
)
from dataagent.actions.tools.local_tool.sub_agent_entry import (
    _build_worker_persistence_state,
    _initial_state_file_has_messages,
    _resolve_subagent_identity,
    _run_agent,
)
from dataagent.actions.tools.local_tool.tools import (
    _run_subprocess_async,
    nl2sql_sub_agent_tool,
    ontology_sub_agent_query_tool,
    reset_subagent_runtime_context,
    set_subagent_runtime_context,
    sub_agent_tool,
)
from dataagent.utils import log as dataagent_log
from dataagent.utils.log import dataagent_logger


@pytest.fixture(autouse=True)
def _bound_sandbox(tmp_path):
    """每个测试自动绑定一个最小 NoopSandbox，满足工具函数的 sandbox 前置要求。"""
    token = set_current_sandbox(NoopSandbox(workspace_root=tmp_path.resolve()))
    yield
    reset_current_sandbox(token)


def test_sub_agent_tool_extracts_structured_json(monkeypatch, tmp_path):
    """子进程返回 ``subagent_final_state`` JSON 时，``state`` 应解析为 dict；``original_msg`` 为 worker_result。"""
    config_file = tmp_path / "sub_agent.yaml"
    config_file.write_text("AGENT_CONFIG:\n  name: test\n", encoding="utf-8")
    captured: dict[str, Any] = {}

    async def _fake_run_subprocess_async(
        cmd, *, timeout, cwd=None, env=None, progress_callback=None, tool_call_id=None
    ):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        captured["initial_state_file"] = cmd[cmd.index("--initial-state-file") + 1]
        sub_id = int(cmd[cmd.index("--sub-id") + 1])
        return {
            "stdout": json.dumps(
                {
                    "error": None,
                    "worker_result": {
                        "sub_id": sub_id,
                        "parent_session_id": "default_session",
                        "worker_session_id": f"subagent_default_session_{sub_id}",
                        "status": "success",
                        "final_answer": "ok",
                        "artifacts": [],
                        "tool_calls_count": 0,
                        "iteration_count": 0,
                        "error": None,
                        "resumed": False,
                    },
                    "worker_persistence": {},
                    "subagent_final_state": '{"intent":"ontology_query","result":{"count":3}}',
                    "assistant_reply": "ok",
                    "sub_id": sub_id,
                },
                ensure_ascii=False,
            ),
            "stderr": "",
            "returncode": 0,
        }

    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools._run_subprocess_async", _fake_run_subprocess_async)

    out = asyncio.run(
        sub_agent_tool(
            query="查询本体",
            config_path=str(config_file),
            timeout=1,
        )
    )

    assert isinstance(out.get("original_msg"), dict)
    assert out["original_msg"]["status"] == "success"
    assert out["original_msg"]["final_answer"] == "ok"
    assert out["state"]["intent"] == "ontology_query"
    assert out["state"]["result"]["count"] == 3
    assert out["frontend_msg"] == "ok"
    cmd = captured["cmd"]
    assert cmd[cmd.index("--user-id") + 1] == "anonymous"
    assert cmd[cmd.index("--session-id") + 1] == "default_session"
    assert "--sub-id" in cmd
    initial_state_file = Path(captured["initial_state_file"])
    assert initial_state_file.is_relative_to(tmp_path)
    assert initial_state_file.parent.parent == tmp_path / ".dataagent_tmp" / "subagents"
    assert not initial_state_file.exists()
    assert not initial_state_file.parent.exists()


def test_sub_agent_tool_uses_contextvar_runtime_context(monkeypatch, tmp_path):
    """通过 set_subagent_runtime_context 注入 user_id / session_id 后，子进程命令应带上对应
    ``--user-id``、``--session-id``。
    """
    config_file = tmp_path / "sub_agent.yaml"
    config_file.write_text("AGENT_CONFIG:\n  name: test\n", encoding="utf-8")
    captured: dict[str, Any] = {}

    async def _fake_run_subprocess_async(
        cmd, *, timeout, cwd=None, env=None, progress_callback=None, tool_call_id=None
    ):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        sub_id = int(cmd[cmd.index("--sub-id") + 1])
        return {
            "stdout": json.dumps(
                {
                    "error": None,
                    "worker_result": {
                        "sub_id": sub_id,
                        "parent_session_id": "main-session",
                        "worker_session_id": f"subagent_main-session_{sub_id}",
                        "status": "success",
                        "final_answer": "ok",
                        "artifacts": [],
                        "tool_calls_count": 0,
                        "iteration_count": 0,
                        "error": None,
                        "resumed": False,
                    },
                    "worker_persistence": {},
                    "assistant_reply": "ok",
                    "sub_id": sub_id,
                },
                ensure_ascii=False,
            ),
            "stderr": "",
            "returncode": 0,
        }

    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools._run_subprocess_async", _fake_run_subprocess_async)
    token = set_subagent_runtime_context(user_id="main-user", session_id="main-session", sub_id=7)

    try:
        out = asyncio.run(
            sub_agent_tool(
                query="查询本体",
                config_path=str(config_file),
                timeout=1,
            )
        )
    finally:
        reset_subagent_runtime_context(token)

    assert isinstance(out["original_msg"], dict)
    assert out["original_msg"]["final_answer"] == "ok"
    assert out["frontend_msg"] == "ok"
    assert captured["timeout"] == 1
    assert "--user-id" in captured["cmd"]
    assert "main-user" in captured["cmd"]
    assert "--session-id" in captured["cmd"]
    assert "main-session" in captured["cmd"]
    assert "--sub-id" in captured["cmd"]


def test_sub_agent_tool_fallback_default_ids_when_no_runtime_context(monkeypatch, tmp_path):
    """未设置 contextvar 时，sub_agent_tool 仍应在命令行中传入
    ``--user-id anonymous``、``--session-id default_session``（缺省回退）。
    """
    config_file = tmp_path / "sub_agent.yaml"
    config_file.write_text("AGENT_CONFIG:\n  name: test\n", encoding="utf-8")
    captured: dict[str, Any] = {}

    async def _fake_run_subprocess_async(
        cmd, *, timeout, cwd=None, env=None, progress_callback=None, tool_call_id=None
    ):
        captured["cmd"] = cmd
        sub_id = int(cmd[cmd.index("--sub-id") + 1])
        return {
            "stdout": json.dumps(
                {
                    "error": None,
                    "worker_result": {
                        "sub_id": sub_id,
                        "parent_session_id": "default_session",
                        "worker_session_id": f"subagent_default_session_{sub_id}",
                        "status": "success",
                        "final_answer": "ok",
                        "artifacts": [],
                        "tool_calls_count": 0,
                        "iteration_count": 0,
                        "error": None,
                        "resumed": False,
                    },
                    "worker_persistence": {},
                    "assistant_reply": "ok",
                    "sub_id": sub_id,
                },
                ensure_ascii=False,
            ),
            "stderr": "",
            "returncode": 0,
        }

    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools._run_subprocess_async", _fake_run_subprocess_async)

    out = asyncio.run(
        sub_agent_tool(
            query="查询本体",
            config_path=str(config_file),
            timeout=1,
        )
    )

    assert isinstance(out["original_msg"], dict)
    assert out["original_msg"]["final_answer"] == "ok"
    assert out["frontend_msg"] == "ok"
    cmd = captured["cmd"]
    assert cmd[cmd.index("--user-id") + 1] == "anonymous"
    assert cmd[cmd.index("--session-id") + 1] == "default_session"
    assert "--sub-id" in cmd


def test_sub_agent_tool_persists_worker_result_and_memory(monkeypatch, tmp_path):
    config_file = tmp_path / "sub_agent.yaml"
    config_file.write_text("AGENT_CONFIG:\n  name: test\n", encoding="utf-8")
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))
    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools.swarm_enabled", lambda _config=None: True)

    async def _fake_run_subprocess_async(
        cmd, *, timeout, cwd=None, env=None, progress_callback=None, tool_call_id=None
    ):
        sub_id = int(cmd[cmd.index("--sub-id") + 1])
        assert cmd[cmd.index("--session-id") + 1] == "main-session"
        return {
            "stdout": json.dumps(
                {
                    "error": None,
                    "worker_result": {
                        "sub_id": sub_id,
                        "parent_session_id": "main-session",
                        "worker_session_id": f"subagent_main-session_{sub_id}",
                        "status": "success",
                        "final_answer": "done",
                        "artifacts": ["/tmp/a.csv"],
                        "tool_calls_count": 2,
                        "iteration_count": 1,
                        "error": None,
                        "resumed": False,
                    },
                    "worker_persistence": {
                        "messages": [
                            {"type": "HumanMessage", "content": "hi"},
                            {"type": "AIMessage", "content": "done"},
                        ],
                        "state": {"status": "success", "iteration_count": 1, "tool_calls_count": 2},
                    },
                    "assistant_reply": "done",
                    "sub_id": sub_id,
                },
                ensure_ascii=False,
            ),
            "stderr": "",
            "returncode": 0,
        }

    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools._run_subprocess_async", _fake_run_subprocess_async)
    token = set_subagent_runtime_context(user_id="main-user", session_id="main-session", sub_id=0)
    try:
        out = asyncio.run(sub_agent_tool("查询本体", str(config_file), sub_id=123456, timeout=1))
    finally:
        reset_subagent_runtime_context(token)

    worker_memory = tmp_path / "dataagent-home" / "main-user" / "main-session" / "workers" / "123456" / ".memory"
    assert out["sub_id"] == 123456
    assert isinstance(out["original_msg"], dict)
    assert out["original_msg"]["status"] == "success"
    assert out["original_msg"]["final_answer"] == "done"
    assert json.loads((worker_memory / "metadata.json").read_text(encoding="utf-8"))["last_answer"] == "done"
    assert json.loads((worker_memory / "metadata.json").read_text(encoding="utf-8"))["last_run_id"] == 0
    assert json.loads((worker_memory / "messages.json").read_text(encoding="utf-8"))["messages"][1]["content"] == "done"
    state_blob = json.loads((worker_memory / "subagent_state.json").read_text(encoding="utf-8"))
    assert state_blob.get("iteration_count") == 1
    assert "worker_session_id" not in state_blob


def test_sub_agent_tool_keeps_state_and_worker_result_contract(monkeypatch, tmp_path):
    """``state`` carries Flex final-state dict; ``original_msg`` is ``worker_result`` for the planner."""
    config_file = tmp_path / "sub_agent.yaml"
    config_file.write_text("AGENT_CONFIG:\n  name: test\n", encoding="utf-8")
    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools.swarm_enabled", lambda _config=None: False)

    async def _fake_run_subprocess_async(
        cmd, *, timeout, cwd=None, env=None, progress_callback=None, tool_call_id=None
    ):
        sub_id = int(cmd[cmd.index("--sub-id") + 1])
        return {
            "stdout": json.dumps(
                {
                    "error": None,
                    "worker_result": {
                        "sub_id": sub_id,
                        "parent_session_id": "default_session",
                        "worker_session_id": f"subagent_default_session_{sub_id}",
                        "status": "success",
                        "final_answer": "done",
                        "artifacts": [],
                        "tool_calls_count": 0,
                        "iteration_count": 1,
                        "error": None,
                        "resumed": False,
                    },
                    "worker_persistence": {},
                    "subagent_final_state": json.dumps(
                        {"sql": "SELECT 1", "columns": ["value"], "rows": [[1]]},
                        ensure_ascii=False,
                    ),
                    "assistant_reply": "done",
                    "sub_id": sub_id,
                },
                ensure_ascii=False,
            ),
            "stderr": "",
            "returncode": 0,
        }

    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools._run_subprocess_async", _fake_run_subprocess_async)

    out = asyncio.run(sub_agent_tool("查询测试", str(config_file), sub_id=123456, timeout=1))

    assert out["original_msg"]["status"] == "success"
    assert out["original_msg"]["final_answer"] == "done"
    assert out["state"]["sql"] == "SELECT 1"
    assert out["state"]["columns"] == ["value"]
    assert out["state"]["rows"] == [[1]]
    assert out["frontend_msg"] == "done"


def test_sub_agent_tool_returns_busy_without_updating_metadata(monkeypatch, tmp_path):
    config_file = tmp_path / "sub_agent.yaml"
    config_file.write_text("AGENT_CONFIG:\n  name: test\n", encoding="utf-8")
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))

    from dataagent.core.swarm.worker_lock import acquire_worker_lock

    lock = acquire_worker_lock(
        user_id="main-user",
        parent_session_id="main-session",
        sub_id=123456,
        query="running",
        ttl_seconds=60,
    )
    assert lock is not None

    async def _should_not_run(*args, **kwargs):
        raise AssertionError("busy worker must not launch subprocess")

    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools._run_subprocess_async", _should_not_run)
    token = set_subagent_runtime_context(user_id="main-user", session_id="main-session", sub_id=0)
    try:
        out = asyncio.run(sub_agent_tool("继续分析", str(config_file), sub_id=123456, timeout=1))
    finally:
        reset_subagent_runtime_context(token)

    assert out["sub_id"] == 123456
    assert isinstance(out["original_msg"], dict)
    assert out["original_msg"]["status"] == "failed"
    assert "create a new subagent" in (out["original_msg"].get("error") or "")
    metadata_path = (
        tmp_path / "dataagent-home" / "main-user" / "main-session" / "workers" / "123456" / ".memory" / "metadata.json"
    )
    assert not metadata_path.exists()


def test_sub_agent_tool_loads_worker_history_when_swarm_enabled_and_assets_exist(monkeypatch, tmp_path):
    """Persisted messages hydrate the initial state when swarm mode is enabled."""
    config_file = tmp_path / "sub_agent.yaml"
    config_file.write_text("AGENT_CONFIG:\n  name: test\n", encoding="utf-8")
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))
    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools.swarm_enabled", lambda _config=None: True)

    from dataagent.core.swarm.worker_memory import persist_worker_messages

    persist_worker_messages(
        user_id="main-user",
        parent_session_id="main-session",
        sub_id=123456,
        messages=[HumanMessage(content="old question"), AIMessage(content="old answer")],
    )
    captured: dict[str, Any] = {}

    async def _fake_run_subprocess_async(
        cmd, *, timeout, cwd=None, env=None, progress_callback=None, tool_call_id=None
    ):
        captured["initial_state_file"] = cmd[cmd.index("--initial-state-file") + 1]
        payload = json.loads(Path(captured["initial_state_file"]).read_text(encoding="utf-8"))
        captured["initial_state"] = payload
        return {
            "stdout": json.dumps(
                {
                    "error": None,
                    "worker_result": {
                        "sub_id": 123456,
                        "parent_session_id": "main-session",
                        "worker_session_id": "subagent_main-session_123456",
                        "status": "success",
                        "final_answer": "new answer",
                        "artifacts": [],
                        "tool_calls_count": 0,
                        "iteration_count": 1,
                        "error": None,
                        "resumed": True,
                    },
                    "worker_persistence": {},
                    "assistant_reply": "new answer",
                    "sub_id": 123456,
                },
                ensure_ascii=False,
            ),
            "stderr": "",
            "returncode": 0,
        }

    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools._run_subprocess_async", _fake_run_subprocess_async)
    token = set_subagent_runtime_context(user_id="main-user", session_id="main-session", sub_id=0)
    try:
        out = asyncio.run(sub_agent_tool("继续", str(config_file), sub_id=123456, timeout=1))
    finally:
        reset_subagent_runtime_context(token)

    assert out["original_msg"]["resumed"] is True
    assert captured["initial_state"]["messages"][0]["content"] == "old question"
    initial_state_file = Path(captured["initial_state_file"])
    assert initial_state_file.is_relative_to(tmp_path)
    assert initial_state_file.parent.parent == tmp_path / ".dataagent_tmp" / "subagents"
    assert not initial_state_file.exists()
    assert not initial_state_file.parent.exists()


def test_sub_agent_tool_warns_and_cold_starts_when_explicit_sub_id_has_no_assets(monkeypatch, tmp_path):
    """Explicit sub_id with no ``.memory`` artifacts logs a warning and starts with empty history."""
    config_file = tmp_path / "sub_agent.yaml"
    config_file.write_text("AGENT_CONFIG:\n  name: test\n", encoding="utf-8")
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))
    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools.swarm_enabled", lambda _config=None: True)

    warnings: list[tuple[Any, ...]] = []

    def _capture_warning(message: str, *args: Any) -> None:
        warnings.append((message, args))

    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools.logger.warning", _capture_warning)

    captured: dict[str, Any] = {}

    async def _fake_run_subprocess_async(
        cmd, *, timeout, cwd=None, env=None, progress_callback=None, tool_call_id=None
    ):
        captured["initial_state_file"] = cmd[cmd.index("--initial-state-file") + 1]
        payload = json.loads(Path(captured["initial_state_file"]).read_text(encoding="utf-8"))
        captured["initial_state"] = payload
        return {
            "stdout": json.dumps(
                {
                    "error": None,
                    "worker_result": {
                        "sub_id": 654321,
                        "parent_session_id": "main-session",
                        "worker_session_id": "subagent_main-session_654321",
                        "status": "success",
                        "final_answer": "fresh answer",
                        "artifacts": [],
                        "tool_calls_count": 0,
                        "iteration_count": 1,
                        "error": None,
                        "resumed": False,
                    },
                    "worker_persistence": {},
                    "assistant_reply": "fresh answer",
                    "sub_id": 654321,
                },
                ensure_ascii=False,
            ),
            "stderr": "",
            "returncode": 0,
        }

    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools._run_subprocess_async", _fake_run_subprocess_async)
    token = set_subagent_runtime_context(user_id="main-user", session_id="main-session", sub_id=0)
    try:
        out = asyncio.run(sub_agent_tool("新问题", str(config_file), sub_id=654321, timeout=1))
    finally:
        reset_subagent_runtime_context(token)

    assert out["sub_id"] == 654321
    assert out["original_msg"]["resumed"] is False
    assert captured["initial_state"]["messages"] == []
    assert captured["initial_state"]["sub_id"] == 654321
    assert warnings
    assert warnings[0][0].startswith("sub_agent_tool: requested sub_id=")
    assert warnings[0][1] == (654321,)


def test_sub_agent_tool_skips_disk_history_when_swarm_disabled(monkeypatch, tmp_path):
    """With swarm disabled, do not hydrate from ``messages.json`` even when present."""
    config_file = tmp_path / "sub_agent.yaml"
    config_file.write_text("AGENT_CONFIG:\n  name: test\n", encoding="utf-8")
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))
    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools.swarm_enabled", lambda _config=None: False)

    from dataagent.core.swarm.worker_memory import persist_worker_messages

    persist_worker_messages(
        user_id="main-user",
        parent_session_id="main-session",
        sub_id=123456,
        messages=[HumanMessage(content="old question"), AIMessage(content="old answer")],
    )
    captured: dict[str, Any] = {}

    async def _fake_run_subprocess_async(
        cmd, *, timeout, cwd=None, env=None, progress_callback=None, tool_call_id=None
    ):
        captured["initial_state_file"] = cmd[cmd.index("--initial-state-file") + 1]
        payload = json.loads(Path(captured["initial_state_file"]).read_text(encoding="utf-8"))
        captured["initial_state"] = payload
        return {
            "stdout": json.dumps(
                {
                    "error": None,
                    "worker_result": {
                        "sub_id": 123456,
                        "parent_session_id": "main-session",
                        "worker_session_id": "subagent_main-session_123456",
                        "status": "success",
                        "final_answer": "new answer",
                        "artifacts": [],
                        "tool_calls_count": 0,
                        "iteration_count": 1,
                        "error": None,
                        "resumed": False,
                    },
                    "worker_persistence": {},
                    "assistant_reply": "new answer",
                    "sub_id": 123456,
                },
                ensure_ascii=False,
            ),
            "stderr": "",
            "returncode": 0,
        }

    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools._run_subprocess_async", _fake_run_subprocess_async)
    token = set_subagent_runtime_context(user_id="main-user", session_id="main-session", sub_id=0)
    try:
        out = asyncio.run(sub_agent_tool("继续", str(config_file), sub_id=123456, timeout=1))
    finally:
        reset_subagent_runtime_context(token)

    assert out["original_msg"]["resumed"] is False
    assert captured["initial_state"]["messages"] == []


def test_initial_state_resumed_flag_depends_on_history_messages(tmp_path):
    """An initial-state file alone is not enough to mark a worker as resumed."""
    empty_state = tmp_path / "empty.json"
    empty_state.write_text(json.dumps({"user_query": "hi", "messages": []}), encoding="utf-8")
    history_state = tmp_path / "history.json"
    history_state.write_text(
        json.dumps({"user_query": "hi", "messages": [{"type": "HumanMessage", "content": "old"}]}),
        encoding="utf-8",
    )

    assert _initial_state_file_has_messages(str(empty_state)) is False
    assert _initial_state_file_has_messages(str(history_state)) is True
    assert _initial_state_file_has_messages(None) is False


def test_worker_persistence_state_keeps_child_fields_only():
    """Persisted worker state snapshots exclude WorkerResult overlays."""
    state = _build_worker_persistence_state(
        jsonable_result={
            "sql": "SELECT 1",
            "query_results": [{"rows": [[1]]}],
            "iteration_count": 3,
            "messages": [{"type": "HumanMessage", "content": "x"}],
        },
    )

    assert state["sql"] == "SELECT 1"
    assert state["query_results"] == [{"rows": [[1]]}]
    assert state["iteration_count"] == 3
    assert "messages" not in state


def test_run_subprocess_async_handles_long_stdout_line_with_progress_callback():
    """进度回调路径下，子进程 stdout 单行超过 asyncio 默认限制时也应完整读取。"""
    payload_size = 70_000
    seen_status: list[str] = []

    result = asyncio.run(
        _run_subprocess_async(
            [
                sys.executable,
                "-c",
                f"import sys; sys.stdout.write('x' * {payload_size})",
            ],
            timeout=5,
            progress_callback=seen_status.append,
            tool_call_id="tool-call-id",
        )
    )

    assert result["returncode"] == 0
    assert result["stdout"] == "x" * payload_size
    assert result["stderr"] == ""
    assert seen_status == []


def test_run_subprocess_async_keeps_stderr_status_progress_callback():
    """分块读取 stderr 后，仍应按行提取子 Agent 状态并触发进度回调。"""
    seen_status: list[tuple[str, str]] = []

    def _record_status(tool_call_id: str, status: str) -> None:
        seen_status.append((tool_call_id, status))

    result = asyncio.run(
        _run_subprocess_async(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('=== Coordinator ===\\nSELECT 1\\n'); sys.stdout.write('ok')",
            ],
            timeout=5,
            progress_callback=_record_status,
            tool_call_id="tool-call-id",
        )
    )

    assert result["returncode"] == 0
    assert result["stdout"] == "ok"
    assert result["stderr"] == "=== Coordinator ===\nSELECT 1"
    assert seen_status == [
        ("tool-call-id", "Coordinator"),
        ("tool-call-id", "Coordinator: SELECT 1"),
    ]


def test_resolve_subagent_identity_matches_runtime_session_name():
    user_id, session_id, sub_id = _resolve_subagent_identity(user_id="main-user", session_id="main-session", sub_id=7)

    assert user_id == "main-user"
    assert session_id == "subagent_main-session_7"
    assert sub_id == 7


def test_run_agent_uses_same_identity_for_logging_and_initial_state(monkeypatch, tmp_path):
    captured: dict[str, Any] = {}

    class _FakeAgent:
        async def chat(self, query: str, initial_state: dict[str, Any]):
            captured["query"] = query
            captured["initial_state"] = dict(initial_state)
            return {"ok": True}

    def _fake_from_config(path: Path):
        captured["config_path"] = path
        return _FakeAgent()

    def _fake_reconfigure(config):
        captured["log_path"] = Path(config.file_path)

    monkeypatch.setattr("dataagent.actions.tools.local_tool.sub_agent_entry.DataAgent.from_config", _fake_from_config)
    monkeypatch.setattr(
        "dataagent.actions.tools.local_tool.sub_agent_entry.dataagent_log.reconfigure", _fake_reconfigure
    )
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))

    result = asyncio.run(
        _run_agent("查询本体", "/tmp/sub_agent.yaml", user_id="main-user", session_id="main-session", sub_id=7)
    )

    assert result == {"ok": True}
    assert captured["query"] == "查询本体"
    assert captured["config_path"] == Path("/tmp/sub_agent.yaml")
    assert (
        captured["log_path"]
        == (tmp_path / "dataagent-home" / "main-user" / "logs" / "subagent_main-session_7_7.log").resolve()
    )
    assert captured["initial_state"]["user_id"] == "main-user"
    assert captured["initial_state"]["session_id"] == "subagent_main-session_7"
    assert captured["initial_state"]["sub_id"] == 7


def test_public_reconfigure_keeps_process_name(monkeypatch):
    captured: dict[str, Any] = {}

    def _fake_reconfigure(config):
        captured["configured_process_name"] = config.process_name

    def _fake_get_logger(process_name=None):
        captured["requested_process_name"] = process_name
        return object()

    monkeypatch.setattr(
        dataagent_logger,
        "_dataagent_logger",
        type(
            "_FakeLogger",
            (),
            {"reconfigure": staticmethod(_fake_reconfigure), "get_logger": staticmethod(_fake_get_logger)},
        )(),
    )
    monkeypatch.setattr(dataagent_logger, "logger", None)

    dataagent_log.reconfigure(
        dataagent_log.LoggerConfig(process_name="subagent", file_path="/tmp/subagent.log", file_path_explicit=True)
    )

    assert captured["configured_process_name"] == "subagent"
    assert captured["requested_process_name"] == "subagent"


def test_nl2sql_sub_agent_tool_overrides_runtime_model_database_and_metavisor(monkeypatch, tmp_path):
    """nl2sql_sub_agent_tool 应按主配置中的 llm_model 等，将运行时的 MODEL / DATABASE / METAVISOR
    写入临时合并后的子 Agent YAML，再交给内部 sub_agent_tool；源 nl2sql 配置磁盘文件不被破坏；
    SQL/CSV 与 frontend_msg 符合预期。
    """
    package_root = tmp_path / "pkg"
    agent_dir = package_root / "agents" / "nl2sql"
    agent_dir.mkdir(parents=True)
    prompts_user = agent_dir / "prompts" / "user"
    prompts_user.mkdir(parents=True)
    (prompts_user / "placeholder.md").write_text("test", encoding="utf-8")
    source_config_path = agent_dir / "nl2sql_agent.yaml"
    main_config_path = tmp_path / "main_agent.yaml"
    workspace = tmp_path / "nl2sql_workspace"
    source_config_path.write_text(
        """
AGENT_CONFIG:
  name: "NL2SQL Agent"
MODEL:
  qwen3_coder:
    model_type: "chat"
    provider: "bailian"
    params:
      model: "qwen3-coder-480b-a35b-instruct"
      temperature: 0.0
DATABASE:
  db_id: "default_db"
  engine: "sqlite"
  config:
    path: "/tmp/default.sqlite"
METAVISOR:
  metavisor_url: "default-metavisor"
  valuematch_url: "default-valuematch"
""".strip(),
        encoding="utf-8",
    )
    main_config_path.write_text(
        """
AGENT_CONFIG:
  name: "Main Agent"
MODEL:
  deepseek:
    model_type: "chat"
    provider: "deepseek"
    params:
      model: "deepseek-chat"
      temperature: 0.0
TOOLS:
  local_functions:
    - module: "dataagent.actions.tools.local_tool.tools"
      function: "nl2sql_sub_agent_tool"
      config:
        llm_model: deepseek
""".strip(),
        encoding="utf-8",
    )

    runtime_database = {
        "db_id": "runtime_db",
        "engine": "sqlite",
        "config": {"host": "127.0.0.1", "port": 5432},
    }
    runtime_metavisor = {
        "metavisor_url": "runtime-metavisor",
        "valuematch_url": "runtime-valuematch",
    }
    runtime_model = {
        "model_type": "chat",
        "provider": "deepseek",
        "params": {
            "model": "deepseek-chat",
            "temperature": 0.0,
        },
    }
    captured: dict[str, Any] = {}

    from dataagent.actions.tools.context import ToolExecutionContext
    from dataagent.config.config_manager import ConfigManager

    agent_cm = ConfigManager()
    agent_cm.config_path = main_config_path
    agent_cm.set("DATABASE", runtime_database)
    agent_cm.set("METAVISOR", runtime_metavisor)
    agent_cm.set("MODEL.deepseek", runtime_model)
    tool_ctx = ToolExecutionContext(
        config_manager=agent_cm,
        tool_config={"llm_model": "deepseek"},
        runtime=SimpleNamespace(workspace_dir=workspace.resolve()),
    )

    async def _fake_sub_agent_tool(query: str, config_path: str, **kwargs):
        captured["query"] = query
        captured["config_path"] = config_path
        captured["config"] = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        return {
            "original_msg": {
                "sub_id": 100001,
                "parent_session_id": "default_session",
                "worker_session_id": "subagent_default_session_100001",
                "status": "success",
                "final_answer": "ok",
                "artifacts": [],
                "tool_calls_count": 0,
                "iteration_count": 0,
                "error": None,
                "resumed": False,
            },
            "frontend_msg": "ok",
            "state": {"sql": "SELECT 1 AS value", "columns": ["value"], "rows": [[1]]},
            "sub_id": 100001,
        }

    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools.dataagent_package_root", lambda: package_root)
    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools.sub_agent_tool", _fake_sub_agent_tool)

    sql_path = workspace / "query.sql"
    csv_path = workspace / "result.csv"
    result = asyncio.run(
        nl2sql_sub_agent_tool(
            query="查询测试",
            sql_filename="query.sql",
            csv_filename="result.csv",
            _tool_context=tool_ctx,
        )
    )

    assert captured["query"] == "查询测试"
    assert captured["config_path"] != str(source_config_path)
    assert captured["config"]["MODEL"] == {"deepseek": runtime_model}
    assert captured["config"]["DATABASE"] == runtime_database
    assert captured["config"]["METAVISOR"] == runtime_metavisor
    assert yaml.safe_load(source_config_path.read_text(encoding="utf-8"))["MODEL"] == {
        "qwen3_coder": {
            "model_type": "chat",
            "provider": "bailian",
            "params": {
                "model": "qwen3-coder-480b-a35b-instruct",
                "temperature": 0.0,
            },
        }
    }
    assert "SELECT 1 AS value" in sql_path.read_text(encoding="utf-8")
    assert "value" in csv_path.read_text(encoding="utf-8")
    assert "SQL 文件已保存到" in result["frontend_msg"]


def test_nl2sql_sub_agent_tool_passes_internal_subagent_context(monkeypatch, tmp_path):
    """在已设置 set_subagent_runtime_context 的前提下调用 nl2sql_sub_agent_tool：query 正确传入，
    流程能完成 SQL/CSV 写入（内部 mock sub_agent_tool），frontend_msg 含「SQL 文件已保存到」。
    """
    package_root = tmp_path / "pkg"
    agent_dir = package_root / "agents" / "nl2sql"
    agent_dir.mkdir(parents=True)
    prompts_user = agent_dir / "prompts" / "user"
    prompts_user.mkdir(parents=True)
    (prompts_user / "placeholder.md").write_text("test", encoding="utf-8")
    source_config_path = agent_dir / "nl2sql_agent.yaml"
    source_config_path.write_text("AGENT_CONFIG:\n  name: test\n", encoding="utf-8")
    workspace = tmp_path / "nl2sql_workspace"
    captured: dict[str, Any] = {}

    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools.dataagent_package_root", lambda: package_root)

    async def _fake_sub_agent_tool(query: str, config_path: str, **kwargs):
        captured["query"] = query
        captured["config_path"] = config_path
        return {
            "original_msg": {
                "sub_id": 100002,
                "parent_session_id": "default_session",
                "worker_session_id": "subagent_default_session_100002",
                "status": "success",
                "final_answer": "ok",
                "artifacts": [],
                "tool_calls_count": 0,
                "iteration_count": 0,
                "error": None,
                "resumed": False,
            },
            "frontend_msg": "ok",
            "state": {"sql": "SELECT 1", "columns": ["value"], "rows": [[1]]},
            "sub_id": 100002,
        }

    from dataagent.actions.tools.context import ToolExecutionContext
    from dataagent.config.config_manager import ConfigManager

    agent_cm = ConfigManager()
    tool_ctx = ToolExecutionContext(
        config_manager=agent_cm,
        runtime=SimpleNamespace(workspace_dir=workspace.resolve()),
    )

    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools.sub_agent_tool", _fake_sub_agent_tool)
    token = set_subagent_runtime_context(user_id="main-user", session_id="main-session", sub_id=8)

    try:
        result = asyncio.run(
            nl2sql_sub_agent_tool(
                query="查询测试",
                sql_filename="query.sql",
                csv_filename="result.csv",
                _tool_context=tool_ctx,
            )
        )
    finally:
        reset_subagent_runtime_context(token)

    assert captured["query"] == "查询测试"
    assert "SQL 文件已保存到" in result["frontend_msg"]


def test_nl2sql_sub_agent_tool_ignores_none_error_field(monkeypatch, tmp_path):
    """A successful NL2SQL payload may include error=None and should still write outputs."""
    package_root = tmp_path / "pkg"
    agent_dir = package_root / "agents" / "nl2sql"
    agent_dir.mkdir(parents=True)
    prompts_user = agent_dir / "prompts" / "user"
    prompts_user.mkdir(parents=True)
    (prompts_user / "placeholder.md").write_text("test", encoding="utf-8")
    source_config_path = agent_dir / "nl2sql_agent.yaml"
    source_config_path.write_text("AGENT_CONFIG:\n  name: test\n", encoding="utf-8")
    workspace = tmp_path / "nl2sql_workspace"

    async def _fake_sub_agent_tool(query: str, config_path: str, **kwargs):
        return {
            "original_msg": {
                "sub_id": 100003,
                "parent_session_id": "default_session",
                "worker_session_id": "subagent_default_session_100003",
                "status": "success",
                "final_answer": "ok",
                "artifacts": [],
                "tool_calls_count": 0,
                "iteration_count": 0,
                "error": None,
                "resumed": False,
            },
            "frontend_msg": "ok",
            "state": {"error": None, "sql": "SELECT 1", "columns": ["value"], "rows": [[1]]},
            "sub_id": 100003,
        }

    from dataagent.actions.tools.context import ToolExecutionContext
    from dataagent.config.config_manager import ConfigManager

    tool_ctx = ToolExecutionContext(
        config_manager=ConfigManager(),
        runtime=SimpleNamespace(workspace_dir=workspace.resolve()),
    )

    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools.dataagent_package_root", lambda: package_root)
    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools.sub_agent_tool", _fake_sub_agent_tool)

    sql_path = workspace / "query.sql"
    csv_path = workspace / "result.csv"
    result = asyncio.run(
        nl2sql_sub_agent_tool(
            query="查询测试",
            sql_filename="query.sql",
            csv_filename="result.csv",
            _tool_context=tool_ctx,
        )
    )

    assert "SQL 文件已保存到" in result["frontend_msg"]
    assert "SELECT 1" in sql_path.read_text(encoding="utf-8")
    assert "value" in csv_path.read_text(encoding="utf-8")


def test_ontology_sub_agent_query_tool_passes_internal_subagent_context(monkeypatch):
    """Mock sub_agent_tool 后，在 runtime context 下调用 ontology_sub_agent_query_tool：
    校验 query、config 路径传入及返回中的 original_msg / frontend_msg 解析。
    """
    captured: dict[str, Any] = {}

    async def _fake_sub_agent_tool(query: str, config_path: str, **kwargs):
        captured["query"] = query
        captured["config_path"] = config_path
        return {
            "original_msg": {
                "sub_id": 100004,
                "parent_session_id": "default_session",
                "worker_session_id": "subagent_default_session_100004",
                "status": "success",
                "final_answer": "ok",
                "artifacts": [],
                "tool_calls_count": 0,
                "iteration_count": 0,
                "error": None,
                "resumed": False,
            },
            "frontend_msg": "ok",
            "state": {"messages": ["done"]},
            "sub_id": 100004,
        }

    from dataagent.actions.tools.context import ToolExecutionContext
    from dataagent.config.config_manager import ConfigManager

    tool_ctx = ToolExecutionContext(config_manager=ConfigManager())

    monkeypatch.setattr("dataagent.actions.tools.local_tool.tools.sub_agent_tool", _fake_sub_agent_tool)
    token = set_subagent_runtime_context(user_id="main-user", session_id="main-session", sub_id=9)

    try:
        result = asyncio.run(ontology_sub_agent_query_tool(query="本体查询", _tool_context=tool_ctx))
    finally:
        reset_subagent_runtime_context(token)

    assert captured["query"] == "本体查询"
    assert result["original_msg"] == "done"
    assert result["frontend_msg"] == "done"
