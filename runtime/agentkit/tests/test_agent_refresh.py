"""Session graph reuse and next-turn extension refresh through the real HTTP host."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage, HumanMessage
from test_api import client_for, query, sse, terminal
from test_home_resources import skill
from test_mcp import stdio

from dataagent import LaunchOptions, extension_revision, prepare_runtime
from restapi import agents


def home_config(runtime, value):
    path = runtime.paths.home / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


async def ask(client, text="hello", thread="thread-1"):
    response = await client.post("/dataagent/stream", json=query(text, thread))
    assert terminal(sse(response)) == ["RUN_FINISHED"]
    return response


async def test_same_session_reuses_graph_and_history_other_sessions_do_not(runtime):
    model = ScriptedModel(responses=[AIMessage(content="done")] * 3)
    with patch.object(agents, "build_agent", wraps=agents.build_agent) as build:
        async with client_for(runtime, model) as (client, app):
            await ask(client, "first")
            await ask(client, "second")
            assert build.call_count == 1
            assert [m.content for m in model.requests[1] if isinstance(m, HumanMessage)] == ["first", "second"]
            assert (await client.get("/sessions/thread-1")).status_code == 200
            assert build.call_count == 1
            await ask(client, "isolated", "thread-2")
            assert build.call_count == 2
            assert len(app.state.agents.graphs) == 2
            assert [m.content for m in model.requests[2] if isinstance(m, HumanMessage)] == ["isolated"]


async def test_skill_install_and_remove_refresh_checkpointed_metadata(runtime):
    model = ScriptedModel(responses=[AIMessage(content="done")] * 3)
    with patch.object(agents, "build_agent", wraps=agents.build_agent) as build:
        async with client_for(runtime, model) as (client, app):
            await ask(client, "before installation")
            installed = skill(runtime.paths.home / "skills", "new-skill", "New skill evidence.")
            await ask(client, "after installation")
            assert "new-skill" in str(model.requests[1][0].content)
            installed.unlink()
            await ask(client, "after removal")
            assert "new-skill" not in str(model.requests[2][0].content)
            assert build.call_count == 3
            graph = next(iter(app.state.agents.graphs.values()))
            state = await graph.aget_state({"configurable": {"thread_id": "thread-1"}})
            assert len([m for m in state.values["messages"] if isinstance(m, HumanMessage)]) == 3
            assert state.values["extension_revision"] == app.state.agents.revision


async def test_register_then_edit_python_hook_without_restart(runtime, tmp_path):
    model = ScriptedModel(responses=[AIMessage(content="done")] * 3)
    output = tmp_path / "audit.txt"
    hook = runtime.paths.home / "hooks/audit.py"

    def source(marker):
        return ("from pathlib import Path\n"
                "def handle(state, runtime, *, params):\n"
                f"    with Path(params['output']).open('a') as stream: stream.write('{marker}')\n")

    async with client_for(runtime, model) as (client, app):
        await ask(client)
        hook.parent.mkdir(parents=True)
        hook.write_text(source("A"))
        home_config(runtime, {"dataagent": {"hooks": {"before_agent": [{
            "entrypoint": "hooks/audit.py:handle", "params": {"output": str(output)},
        }]}}})
        await ask(client)
        hook.write_text(source("B"))  # Same-size, potentially same-second Python source edit.
        await ask(client)
        assert output.read_text() == "AB"


async def test_install_enabled_plugin_and_remove_its_tools(runtime):
    model = ScriptedModel(responses=[
        AIMessage(content="initial"), call("extra__ping", {}), AIMessage(content="installed"),
        AIMessage(content="removed"),
    ])
    async with client_for(runtime, model) as (client, app):
        await ask(client)
        plugin = runtime.paths.home / "plugins/extra"
        plugin.mkdir(parents=True)
        (plugin / "tools.py").write_text('def ping() -> str:\n    """Return plugin evidence."""\n    return "pong"\n')
        (plugin / ".plugin.json").write_text(json.dumps({
            "id": "extra", "tools": {"ping": {"entrypoint": "tools.py:ping"}},
        }))
        home_config(runtime, {"plugins": {"enabled": ["common", "extra"]}})
        await ask(client)
        assert "extra__ping" in model.tool_schemas[-1]
        assert (await client.get("/healthz")).json()["plugins"] == ["common", "extra"]
        home_config(runtime, {"plugins": {"enabled": ["common"]}})
        await ask(client)
        assert "extra__ping" not in model.tool_schemas[-1]


async def test_reload_failure_preserves_previous_session_and_retries_after_fix(runtime):
    model = ScriptedModel(responses=[AIMessage(content="done")] * 2)
    async with client_for(runtime, model) as (client, app):
        await ask(client)
        previous = app.state.agents.revision
        graph = next(iter(app.state.agents.graphs.values()))
        home_config(runtime, {"plugins": {"enabled": ["missing-plugin"]}})
        response = await client.post("/dataagent/stream", json=query("rejected"))
        assert response.status_code == 409 and "Extension reload failed" in response.text
        assert app.state.agents.revision == previous
        assert next(iter(app.state.agents.graphs.values())) is graph
        restored = (await client.get("/sessions/thread-1")).json()
        assert restored["status"] == "complete"
        home_config(runtime, {"plugins": {"enabled": ["common"]}})
        await ask(client, "recovered")
        assert [m.content for m in model.requests[-1] if isinstance(m, HumanMessage)] == ["hello", "recovered"]


async def test_mcp_install_reuses_discovery_until_connection_changes(runtime):
    model = ScriptedModel(responses=[
        AIMessage(content="initial"), call("mcp__user__calc_add", {"a": 1, "b": 2}),
        AIMessage(content="three"), AIMessage(content="unchanged"), AIMessage(content="removed"),
    ])
    discover = AsyncMock(wraps=agents.load_mcp_tools)
    with patch.object(agents, "load_mcp_tools", discover):
        async with client_for(runtime, model) as (client, app):
            await ask(client)
            path = runtime.paths.home / ".mcp.json"
            path.write_text(json.dumps({"mcpServers": {"calc": stdio()}}))
            await ask(client)
            assert discover.await_count == 1
            await ask(client)
            assert discover.await_count == 1
            path.write_text('{"mcpServers": {}}')
            await ask(client)
            assert discover.await_count == 2
            assert "mcp__user__calc_add" not in model.tool_schemas[-1]


async def test_explicit_env_file_is_preserved_and_host_settings_are_not_hot_changed(runtime, tmp_path):
    selected = tmp_path / "selected.env"
    selected.write_text("MCP_TEST_TOKEN=first\n")
    runtime = prepare_runtime(LaunchOptions(config=runtime.report.configs[-1].path, env_file=selected))
    model = ScriptedModel(responses=[AIMessage(content="done")] * 2)
    discover = AsyncMock(return_value=[])
    with patch.object(agents, "load_mcp_tools", discover):
        async with client_for(runtime, model) as (client, app):
            await ask(client)
            home_config(runtime, {"server": {"port": 9000}, "dataagent": {"limits": {"timeout_seconds": 999}}})
            (runtime.paths.home / ".mcp.json").write_text(json.dumps({"mcpServers": {"remote": {
                "type": "http", "url": "https://example.invalid/mcp",
                "headers": {"Authorization": "Bearer $env{MCP_TEST_TOKEN}"},
            }}}))
            selected.write_text("MCP_TEST_TOKEN=second\n")
            await ask(client)
            candidate = discover.call_args.args[0]
            assert candidate.extensions.mcp_servers["mcp__user__remote"]["headers"]["Authorization"] == "Bearer second"
            assert candidate.settings.server == runtime.settings.server
            assert candidate.timeout_seconds == runtime.timeout_seconds
            assert candidate.paths.home == runtime.paths.home


async def test_active_run_keeps_graph_while_another_session_refreshes(runtime):
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingModel(ScriptedModel):
        async def _agenerate(self, messages, *args, **kwargs):
            if messages[-1].content == "hold":
                entered.set()
                await release.wait()
            return await super()._agenerate(messages, *args, **kwargs)

    model = WaitingModel(responses=[AIMessage(content="done")] * 2)
    async with client_for(runtime, model) as (client, app):
        running = asyncio.create_task(ask(client, "hold", "old"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            skill(runtime.paths.home / "skills", "new-skill")
            await ask(client, "new run", "new")
            assert "new-skill" in str(model.requests[0][0].content)
            conflict = await client.post("/dataagent/stream", json=query(thread="old"))
            assert conflict.status_code == 409
        finally:
            release.set()
            await running
        assert "new-skill" not in str(model.requests[1][0].content)


async def test_cache_eviction_keeps_persisted_history(runtime):
    model = ScriptedModel(responses=[AIMessage(content="done")] * 3)
    with patch.object(agents, "build_agent", wraps=agents.build_agent) as build:
        async with client_for(runtime, model) as (client, app):
            app.state.agents.max_cached = 1
            await ask(client, "first", "one")
            await ask(client, "second", "two")
            await ask(client, "third", "one")
            assert len(app.state.agents.graphs) == 1 and build.call_count == 3
            assert [m.content for m in model.requests[-1] if isinstance(m, HumanMessage)] == ["first", "third"]


async def test_restart_refreshes_old_skill_metadata_without_losing_history(runtime):
    async with client_for(runtime, ScriptedModel(responses=[AIMessage(content="done")])) as (client, _):
        await ask(client, "before restart")
    skill(runtime.paths.home / "skills", "installed-while-stopped")
    refreshed = prepare_runtime(LaunchOptions(config=runtime.report.configs[-1].path))
    model = ScriptedModel(responses=[AIMessage(content="done")])
    async with client_for(refreshed, model) as (client, _):
        await ask(client, "after restart")
    assert "installed-while-stopped" in str(model.requests[0][0].content)
    assert [m.content for m in model.requests[0] if isinstance(m, HumanMessage)] == ["before restart", "after restart"]


async def test_mcp_reload_failure_does_not_publish_tools_or_expose_new_credentials(runtime):
    model = ScriptedModel(responses=[AIMessage(content="done")])
    async with client_for(runtime, model) as (client, app):
        await ask(client)
        previous = app.state.agents.runtime
        tools = app.state.agents.mcp_tools
        (runtime.paths.home / ".mcp.json").write_text(json.dumps({"mcpServers": {"remote": {
            "type": "http", "url": "https://example.invalid/mcp",
            "headers": {"Authorization": "synthetic-new-secret"},
        }}}))
        with patch.object(agents, "load_mcp_tools", AsyncMock(side_effect=ValueError("bad synthetic-new-secret"))):
            response = await client.post("/dataagent/stream", json=query("rejected"))
        assert response.status_code == 409 and "Extension reload failed" in response.text
        assert "synthetic-new-secret" not in response.text
        assert app.state.agents.runtime is previous
        assert app.state.agents.mcp_tools is tools


async def test_cancelling_reload_releases_preparation_lock_and_session_busy_flag(runtime):
    entered = asyncio.Event()

    async def wait_for_discovery(candidate):
        entered.set()
        await asyncio.Event().wait()

    model = ScriptedModel(responses=[AIMessage(content="done")] * 2)
    async with client_for(runtime, model) as (client, app):
        await ask(client)
        previous = app.state.agents.runtime
        path = runtime.paths.home / ".mcp.json"
        path.write_text(json.dumps({"mcpServers": {"calc": stdio()}}))
        with patch.object(agents, "load_mcp_tools", wait_for_discovery):
            request = asyncio.create_task(client.post("/dataagent/stream", json=query("cancelled")))
            try:
                await asyncio.wait_for(entered.wait(), 5)
            finally:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
        assert app.state.agents.runtime is previous
        assert not app.state.agents.lock.locked()
        path.write_text('{"mcpServers": {}}')
        await ask(client, "recovered")
        assert [m.content for m in model.requests[-1] if isinstance(m, HumanMessage)] == ["hello", "recovered"]


def test_revision_ignores_business_outputs_and_bytecode(runtime):
    revision = extension_revision(runtime)
    outputs = runtime.paths.home / "runtime/users/default/sessions/test/outputs"
    outputs.mkdir(parents=True)
    (outputs / "report.csv").write_text("generated data")
    assert extension_revision(runtime) == revision
    installed = skill(runtime.paths.home / "skills", "new-skill")
    changed = extension_revision(runtime)
    assert changed != revision
    cache = installed.parent / "__pycache__"
    cache.mkdir()
    (cache / "ignored.pyc").write_bytes(b"bytecode")
    assert extension_revision(runtime) == changed
