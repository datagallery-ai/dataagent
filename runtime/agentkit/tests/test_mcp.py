"""Configuration boundaries and real MCP tools through native SDK/AG-UI paths."""

import asyncio
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage, ToolMessage
from process_helpers import stop_child
from test_api import client_for, query, sse, terminal
from test_launch import MODEL, config
from test_launcher import config_file

from dataagent import LaunchOptions, build_agent, load_mcp_tools, prepare_runtime
from dataagent.agent import load_extensions

SERVER = Path(__file__).parent / "fixtures/mcp_server.py"


def stdio(**changes):
    return {"command": sys.executable, "args": [str(SERVER)], **changes}


def prepare(tmp_path, servers=None, **options):
    home = tmp_path / "home"
    config(home / "config.json", MODEL)
    if servers is not None:
        config(home / ".mcp.json", {"mcpServers": servers})
    return prepare_runtime(LaunchOptions(cwd=tmp_path, **options))


def invoke_tool(tool, args=None):
    return tool.ainvoke({"type": "tool_call", "id": "mcp-test-call", "name": tool.name, "args": args or {}})


def by_name(tools, name):
    return next(tool for tool in tools if tool.name == name)


def test_no_mcp_file_means_no_configuration_or_new_files(tmp_path):
    runtime = prepare(tmp_path)
    assert runtime.extensions.mcp_servers == {}
    assert not (runtime.paths.home / ".mcp.json").exists()
    assert not (runtime.paths.home / "runtime").exists()


def test_two_sources_only_disabled_plugins_and_adjacent_configs_ignored(tmp_path):
    home = tmp_path / "home"
    for root in (tmp_path, tmp_path / ".dataagent", tmp_path / "input", home / "plugins/off"):
        root.mkdir(parents=True, exist_ok=True)
        (root / ".mcp.json").write_text("not json")
    plugin = home / "plugins/analytics"
    config(plugin / ".plugin.json", {"id": "analytics"})
    config(plugin / ".mcp.json", {"mcpServers": {"calc": stdio()}})
    external = config(tmp_path / "config.json", {"plugins": {"enabled": ["analytics"]}})
    runtime = prepare(tmp_path, {"calc": stdio()}, config=external, workspace=tmp_path / "input")
    assert list(runtime.extensions.mcp_servers) == ["mcp__user__calc", "mcp__analytics__calc"]
    assert runtime.extensions.mcp_servers["mcp__analytics__calc"]["cwd"] == str(plugin)
    with pytest.raises(TypeError):
        runtime.extensions.mcp_servers["new"] = {}


def test_env_uses_existing_layers_and_relative_command_anchors_to_mcp_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("MCP_TEST_VALUE=home-secret\n")
    explicit = tmp_path / "selected.env"
    explicit.write_text("MCP_TEST_VALUE=file-secret\n")
    runtime = prepare(tmp_path, {"calc": stdio(command="./bin/server", env={"VALUE": "$env{MCP_TEST_VALUE}"})}, env_file=explicit)
    connection = runtime.extensions.mcp_servers["mcp__user__calc"]
    assert connection["command"] == str(home / "bin/server")
    assert connection["env"] == {"VALUE": "file-secret"}
    assert "file-secret" not in repr(runtime)
    assert "file-secret" in runtime.redaction_secrets
    monkeypatch.setenv("MCP_TEST_VALUE", "process-secret")
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, env_file=explicit))
    assert runtime.extensions.mcp_servers["mcp__user__calc"]["env"]["VALUE"] == "process-secret"


@pytest.mark.parametrize("entry", [
    {"type": "http"}, {"type": "http", "url": "not-a-url"},
    {"type": "ws", "url": "wss://example.invalid"},
    {"command": "python", "cwd": "/tmp"}, {"command": "python", "disabled": True},
    {"command": "python", "args": "server.py"}, {"command": "python", "env": {"KEY": 3}},
    {"command": "python", "env": {"KEY": "$env{MISSING_MCP_TEST_VALUE}"}},
    {"type": "http", "url": "http://localhost/mcp", "command": "python"},
])
def test_invalid_config_fails_before_connection(tmp_path, entry):
    with pytest.raises(ValueError):
        prepare(tmp_path, {"calc": entry})


@pytest.mark.parametrize("body", [
    '{"mcpServers":{},"mcpServers":{}}', '{"mcpServers":{},"extra":true}',
    '{"mcpServers":{"bad name":{"command":"python"}}}', '{}',
])
def test_strict_json_and_server_names(tmp_path, body):
    prepare(tmp_path)
    (tmp_path / "home/.mcp.json").write_text(body)
    with pytest.raises(ValueError):
        prepare_runtime(LaunchOptions(cwd=tmp_path))


async def test_stdio_discovery_calls_errors_and_process_cleanup(tmp_path):
    home = tmp_path / "home"
    runtime = prepare(tmp_path, {"calc": stdio(args=["server.py"], env={
        "MCP_TEST_VALUE": "synthetic-env", "MCP_TEST_PIDS": str(tmp_path / "pids"),
    })})
    shutil.copyfile(SERVER, home / "server.py")
    tools = await load_mcp_tools(runtime)
    result = await invoke_tool(by_name(tools, "mcp__user__calc_add"), {"a": 2, "b": 3})
    assert isinstance(result, ToolMessage) and result.status == "success"
    assert "5" in str(result.content)
    result = await invoke_tool(by_name(tools, "mcp__user__calc_context"))
    assert str(home) in str(result.content) and "synthetic-env" in str(result.content)
    # A fresh native MCP session/process is used for each call.
    assert '"calls": 0' in str(result.content)
    error = await invoke_tool(by_name(tools, "mcp__user__calc_fail"))
    assert error.status == "error" and "synthetic MCP tool failure" in str(error.content)
    pids = (tmp_path / "pids").read_text().splitlines()
    assert len(pids) == 4
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid), 0)


async def test_sdk_requires_explicit_async_loading_then_uses_native_tools(tmp_path):
    runtime = prepare(tmp_path, {"calc": stdio()})
    with pytest.raises(ValueError, match="await load_mcp_tools"):
        build_agent(runtime, model=ScriptedModel(responses=[]))
    model = ScriptedModel(responses=[
        call("mcp__user__calc_add", {"a": 4, "b": 5}), AIMessage(content="9"),
    ])
    tools = await load_mcp_tools(runtime)
    result = await build_agent(runtime, model=model, mcp_tools=tools).ainvoke({"messages": [("user", "add")]})
    message = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    assert message.name == "mcp__user__calc_add" and message.status == "success"
    assert "9" in str(message.content)


def test_documented_full_example_parses_all_transports(tmp_path, monkeypatch):
    prepare(tmp_path)
    example = Path(__file__).resolve().parents[1] / "examples/mcp/mcp.full.json"
    shutil.copyfile(example, tmp_path / "home/.mcp.json")
    monkeypatch.setenv("MCP_PYTHON", sys.executable)
    monkeypatch.setenv("ANALYTICS_MCP_TOKEN", "synthetic-token")
    monkeypatch.setenv("LEGACY_MCP_API_KEY", "synthetic-key")

    # Validate configuration only: example.com endpoints are placeholders.
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path))
    servers = runtime.extensions.mcp_servers
    assert set(servers) == {"mcp__user__math", "mcp__user__analytics", "mcp__user__legacy"}
    assert servers["mcp__user__math"]["transport"] == "stdio"
    assert servers["mcp__user__math"]["command"] == sys.executable
    assert servers["mcp__user__math"]["args"] == ("math_server.py",)
    assert servers["mcp__user__math"]["env"] == {"PYTHONUNBUFFERED": "1"}
    assert servers["mcp__user__analytics"]["transport"] == "streamable_http"
    assert servers["mcp__user__analytics"]["headers"] == {"Authorization": "Bearer synthetic-token"}
    assert servers["mcp__user__legacy"]["transport"] == "sse"
    assert servers["mcp__user__legacy"]["headers"] == {"X-API-Key": "synthetic-key"}


async def test_documented_local_example_is_runnable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    prepare(tmp_path)
    example = Path(__file__).resolve().parents[1] / "examples/mcp"
    shutil.copyfile(example / ".mcp.json", home / ".mcp.json")
    shutil.copyfile(example / "math_server.py", home / "math_server.py")
    monkeypatch.setenv("MCP_PYTHON", sys.executable)
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path))
    tools = await load_mcp_tools(runtime)
    result = await invoke_tool(by_name(tools, "mcp__user__math_add"), {"a": 12, "b": 30})
    assert result.status == "success" and "42" in str(result.content)


def test_real_backend_discovers_mcp_before_ready_then_exits_with_parent(tmp_path):
    prepare(tmp_path, {"calc": stdio(env={"MCP_TEST_PIDS": str(tmp_path / "pids")})})
    path, _ = config_file(tmp_path)
    child = subprocess.Popen([
        sys.executable, "-m", "restapi", "serve", "--stdio-ready", "--config", str(path),
    ], env={**os.environ, "DATAAGENT_V2_INSTANCE_ID": "mcp-startup-test"},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        assert select.select([child.stdout], [], [], 15)[0], "No readiness message"
        ready = json.loads(child.stdout.readline())
        assert ready["type"] == "ready" and ready["instanceId"] == "mcp-startup-test"
        pids = (tmp_path / "pids").read_text().splitlines()
        assert len(pids) == 1  # Discovery occurred before the parent was notified.
        for pid in pids:
            with pytest.raises(ProcessLookupError):
                os.kill(int(pid), 0)
        child.stdin.close()
        assert child.wait(timeout=10) == 0
    finally:
        stop_child(child, process_group=True)
        for pipe in (child.stdin, child.stdout, child.stderr):
            pipe.close()


async def test_rest_tool_error_and_session_restore_use_native_messages(tmp_path):
    runtime = prepare(tmp_path, {"calc": stdio()})
    model = ScriptedModel(responses=[call("mcp__user__calc_fail", {}), AIMessage(content="Tool failed")])
    async with client_for(runtime, model) as (client, app):
        discovered = app.state.agents.mcp_tools
        events = sse(await client.post("/dataagent/stream", json=query()))
        assert terminal(events) == ["RUN_FINISHED"]  # Model handled a tool-domain error.
        assert any(event["type"] == "TOOL_CALL_RESULT" for event in events)
        restored = (await client.get("/sessions/thread-1")).json()["messages"]
        error = next(item for item in restored if item["role"] == "tool")
        assert "synthetic MCP tool failure" in str(error["content"])
        assert app.state.agents.mcp_tools is discovered
        assert any(isinstance(m, ToolMessage) and m.status == "error" for m in model.requests[-1])


async def test_discovery_failure_prevents_ready(tmp_path):
    runtime = prepare(tmp_path, {"missing": stdio(command="/nonexistent/dataagent-mcp-test")})
    with pytest.raises(ValueError, match="MCP discovery failed for mcp__user__missing"):
        async with client_for(runtime, ScriptedModel(responses=[])):
            pytest.fail("Backend became ready with missing MCP tools")


async def test_cancelled_tool_call_closes_stdio_process(tmp_path):
    started = tmp_path / "tool-started"
    runtime = prepare(tmp_path, {"calc": stdio(env={
        "MCP_TEST_PIDS": str(tmp_path / "pids"), "MCP_TEST_TOOL_STARTED": str(started),
    })})
    tools = await load_mcp_tools(runtime)
    task = asyncio.create_task(invoke_tool(by_name(tools, "mcp__user__calc_slow")))
    async with asyncio.timeout(10):
        # Cancel the actual tool call, not the peer's initialization handshake.
        while not started.exists():
            await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for pid in (tmp_path / "pids").read_text().splitlines():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid), 0)


async def test_transport_failure_becomes_run_error(tmp_path):
    runtime = prepare(tmp_path, {"calc": stdio()})
    model = ScriptedModel(responses=[call("mcp__user__calc_disconnect", {})])
    async with client_for(runtime, model) as (client, _):
        assert terminal(sse(await client.post("/dataagent/stream", json=query()))) == ["RUN_ERROR"]
        assert len(model.requests) == 1


async def test_discovery_timeout_cleans_up_started_process(tmp_path):
    runtime = prepare(tmp_path, {"calc": stdio(env={
        "MCP_TEST_PIDS": str(tmp_path / "pids"), "MCP_TEST_START_DELAY": "30",
    })})
    settings = runtime.settings
    runtime = replace(runtime, settings=settings.model_copy(update={
        "dataagent": settings.dataagent.model_copy(update={
            "limits": settings.dataagent.limits.model_copy(update={"timeout_seconds": 1}),
        }),
    }))
    with pytest.raises(ValueError, match="MCP discovery timed out at mcp__user__calc"):
        await load_mcp_tools(runtime)
    pids = (tmp_path / "pids").read_text().splitlines()
    assert pids
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid), 0)


async def test_plugin_subagent_allowlist_hooks_and_namespaces(tmp_path):
    home = tmp_path / "home"
    plugin = home / "plugins/analytics"
    config(plugin / ".plugin.json", {"id": "analytics", "subagents": ["child.json"]})
    config(plugin / ".mcp.json", {"mcpServers": {"calc": stdio()}})
    (plugin / "prompt.md").write_text("Use the configured MCP calculator.")
    hook = plugin / "audit.py"
    hook.write_text(
        "from pathlib import Path\n"
        "async def before(request, *, params):\n"
        "    Path(params['file']).write_text(request.tool_call['name'])\n"
    )
    marker = tmp_path / "hook-called"
    config(plugin / "child.json", {
        "name": "analyst", "description": "Use MCP", "system_prompt": "prompt.md",
        "tools": ["mcp__analytics__calc_add"], "hooks": {"before_tool": [{
            "entrypoint": "audit.py:before", "matcher": "mcp__analytics__calc_add",
            "params": {"file": str(marker)},
        }]},
    })
    explicit = config(tmp_path / "override.json", {"plugins": {"enabled": ["analytics"]}})
    runtime = prepare(tmp_path, {"calc": stdio()}, config=explicit)
    tools = await load_mcp_tools(runtime)
    assert by_name(tools, "mcp__user__calc_add") and by_name(tools, "mcp__analytics__calc_add")
    compiled = load_extensions(runtime, mcp_tools=tools)
    assert [tool.name for tool in compiled["subagents"][0]["tools"]] == ["mcp__analytics__calc_add"]
    model = ScriptedModel(responses=[
        call("task", {"subagent_type": "analyst", "description": "Add 6 and 7"}),
        call("mcp__analytics__calc_add", {"a": 6, "b": 7}),
        AIMessage(content="13"), AIMessage(content="13"),
    ])
    result = await build_agent(runtime, mcp_tools=tools, model=model).ainvoke({"messages": [("user", "delegate")]})
    assert marker.read_text() == "mcp__analytics__calc_add"
    assert result["messages"][-1].content == "13"
    # An explicit empty child list must not inherit MCP tools.
    child = json.loads((plugin / "child.json").read_text())
    config(plugin / "child.json", {**child, "tools": []})
    assert load_extensions(runtime, mcp_tools=tools)["subagents"][0]["tools"] == []


async def test_general_purpose_delegate_inherits_mcp_tools(tmp_path):
    runtime = prepare(tmp_path, {"calc": stdio()})
    model = ScriptedModel(responses=[
        call("task", {"subagent_type": "general-purpose", "description": "Add 1 and 2"}),
        call("mcp__user__calc_add", {"a": 1, "b": 2}), AIMessage(content="3"), AIMessage(content="3"),
    ])
    graph = build_agent(runtime, model=model, mcp_tools=await load_mcp_tools(runtime))
    await graph.ainvoke({"messages": [("user", "delegate")]})
    assert any(isinstance(message, ToolMessage) and message.name == "mcp__user__calc_add"
               for request in model.requests for message in request)


@pytest.mark.parametrize("names", [["same", "same"], ["invalid.name"], ["x" * 65]])
async def test_invalid_or_duplicate_final_tool_names_fail(tmp_path, monkeypatch, names):
    from langchain_core.tools import tool
    from langchain_mcp_adapters.client import MultiServerMCPClient

    @tool
    def sample() -> str:
        """Synthetic tool."""
        return "ok"

    async def fake_get_tools(self, **kwargs):
        return [sample.model_copy(update={"name": name}) for name in names]

    monkeypatch.setattr(MultiServerMCPClient, "get_tools", fake_get_tools)
    with pytest.raises(ValueError, match="MCP"):
        await load_mcp_tools(prepare(tmp_path, {"calc": stdio()}))


@pytest.mark.parametrize("transport,endpoint", [("http", "/mcp"), ("sse", "/sse")])
async def test_remote_transports_and_configured_headers(tmp_path, transport, endpoint):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    child = subprocess.Popen([sys.executable, str(SERVER), transport, str(port)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while True:
            assert child.poll() is None
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                assert time.monotonic() < deadline
                await asyncio.sleep(0.02)
        runtime = prepare(tmp_path, {"calc": {
            "type": transport, "url": f"http://127.0.0.1:{port}{endpoint}",
            "headers": {"Authorization": "Bearer test-mcp-token"},
        }})
        tools = await load_mcp_tools(runtime)
        result = await invoke_tool(by_name(tools, "mcp__user__calc_add"), {"a": 2, "b": 7})
        assert result.status == "success" and "9" in str(result.content)
    finally:
        stop_child(child)
