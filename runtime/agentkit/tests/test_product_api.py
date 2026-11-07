"""Product HTTP surface: health envelope, sessions, conversation, and bind settings."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from conftest import ScriptedModel
from langchain_core.messages import AIMessage

from dataagent.settings import ServerSettings
from serve.app import create_app


@pytest.fixture
async def app(runtime):
    application = create_app(runtime, model=ScriptedModel(responses=[AIMessage(content="ok")]))
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8787", timeout=30,
    ) as http:
        yield http


async def test_healthz_returns_ok_envelope(client) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] == "ok"
    assert body["data"]["conversation"]["ready"] is True
    assert body["data"]["conversation"]["protocol"] == "dataagent"


async def test_ready_returns_control_plane_status(client) -> None:
    response = await client.get("/ready")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "ready"
    assert data["control_plane"] == "ready"
    assert data["conversation"]["ready"] is True


async def test_config_and_auth_routes_are_absent(client) -> None:
    for path in (
        "/api/v1/auth/status",
        "/api/v1/capabilities",
        "/api/v1/workspace-config",
        "/api/v1/run-defaults",
        "/api/v1/datasource-types",
    ):
        response = await client.get(path)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_unknown_route_uses_error_envelope(client) -> None:
    response = await client.get("/missing")
    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_me_uses_the_runtime_user(client, app) -> None:
    response = await client.get("/api/v1/me")
    assert response.status_code == 200
    user = response.json()["data"]["user"]
    assert user["id"] == app.state.agents.runtime.paths.user_id
    assert user["displayName"] == user["id"]


async def test_session_list_reads_the_agentkit_index(client, app) -> None:
    user_id = app.state.agents.runtime.paths.user_id
    await app.state.sessions.start(user_id, "thread-a", "run-a", "A title")
    await app.state.sessions.finish(user_id, "thread-a", "complete", "ckpt-a")
    await app.state.sessions.start("someone-else", "thread-b", "run-b", "B title")

    listed = await client.get("/api/v1/sessions")
    assert listed.status_code == 200, listed.text
    sessions = listed.json()["data"]["sessions"]
    assert [item["threadId"] for item in sessions] == ["thread-a"]
    assert sessions[0]["id"] == "thread-a"
    assert sessions[0]["title"] == "A title"
    assert sessions[0]["titleSource"] == "fallback"
    assert sessions[0]["hasCheckpoint"] is True
    assert "user_id" not in sessions[0]
    terminal = await client.get("/sessions")
    assert [item["threadId"] for item in terminal.json()["sessions"]] == ["thread-a"]


async def test_session_limit_is_validated(client) -> None:
    for raw in ("0", "101", "abc"):
        response = await client.get("/api/v1/sessions", params={"limit": raw})
        assert response.status_code == 422, response.text
    empty = await client.get("/api/v1/sessions", params={"limit": "10"})
    assert empty.status_code == 200
    assert empty.json()["data"]["sessions"] == []


def _gui(text: str, thread: str) -> dict:
    return {
        "threadId": thread,
        "runId": str(uuid4()),
        "messages": [{"id": str(uuid4()), "role": "user", "content": text}],
        "tools": [],
        "context": [],
        "state": {},
        "forwardedProps": {},
    }


def _texts(payload: dict) -> list[str]:
    texts: list[str] = []
    for item in payload["messages"]:
        content = item.get("content")
        if isinstance(content, str):
            texts.append(content)
    return texts


async def test_missing_conversation_matches_native_session(client) -> None:
    response = await client.get("/api/v1/sessions/missing-thread/conversation")
    native = await client.get("/sessions/missing-thread")
    assert response.status_code == native.status_code == 404


async def test_web_and_tui_writes_are_readable(runtime) -> None:
    scripted = ScriptedModel(responses=[
        AIMessage(content="from-web"),
        AIMessage(content="from-tui"),
    ])
    application = create_app(runtime, model=scripted)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8787", timeout=30,
        ) as http:
            web = await http.post("/dataagent/stream", json=_gui("来自网页", "web-thread"))
            tui = await http.post("/dataagent/stream", json=_gui("来自终端", "tui-thread"))
            web_view = await http.get("/api/v1/sessions/web-thread/conversation")
            tui_view = await http.get("/api/v1/sessions/tui-thread/conversation")
            user_id = (await http.get("/api/v1/me")).json()["data"]["user"]["id"]
            application.state.conversation_busy.add((user_id, "web-thread"))
            running = await http.get("/api/v1/sessions/web-thread/conversation")

    assert web.status_code == 200, web.text
    assert tui.status_code == 200, tui.text
    assert web_view.status_code == 200
    assert tui_view.status_code == 200
    web_body = web_view.json()["data"]
    tui_body = tui_view.json()["data"]
    assert web_body["threadId"] == "web-thread"
    assert "来自网页" in _texts(web_body)
    assert "from-web" in _texts(web_body)
    assert "来自终端" in _texts(tui_body)
    assert "from-tui" in _texts(tui_body)
    assert running.status_code == 200
    assert running.json()["data"]["threadId"] == "web-thread"
    assert "来自网页" in _texts(running.json()["data"])


async def test_agent_messages_keep_tool_calls_in_checkpoint_order(runtime) -> None:
    tool_call = AIMessage(
        content="",
        tool_calls=[{
            "name": "ls",
            "args": {"path": "."},
            "id": "call-ls",
            "type": "tool_call",
        }],
    )
    scripted = ScriptedModel(responses=[tool_call, AIMessage(content="after-tool")])
    application = create_app(runtime, model=scripted)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8787", timeout=30,
        ) as http:
            posted = await http.post("/dataagent/stream", json=_gui("tool-1", "tool-thread"))
            view = await http.get("/api/v1/sessions/tool-thread/conversation")
            native = await http.get("/sessions/tool-thread")
    assert posted.status_code == 200, posted.text
    assert view.status_code == 200, view.text
    assert view.json()["data"] == native.json()
    roles = [message.get("role") for message in view.json()["data"]["messages"]]
    assert "tool" in roles
    assert any(
        message.get("toolCalls") or message.get("tool_calls")
        for message in view.json()["data"]["messages"]
    )


def test_model_settings_come_from_home_env(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        "LLM_MODEL=from-home\nLLM_BASE_URL=https://home.invalid/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATAAGENT_HOME", str(home))
    for key in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "DATAAGENT_WORKSPACE"):
        monkeypatch.delenv(key, raising=False)

    from dataagent import LaunchOptions, prepare_runtime

    with pytest.raises(ValueError):
        prepare_runtime(LaunchOptions(init_home=True))

    (home / ".env").write_text(
        "LLM_MODEL=from-home\nLLM_BASE_URL=https://home.invalid/v1\nLLM_API_KEY=home-key\n",
        encoding="utf-8",
    )
    runtime = prepare_runtime(LaunchOptions(init_home=True))
    assert runtime.model_name == "from-home"


async def test_control_plane_serves_conversation_on_the_agentkit_routes(client) -> None:
    for path in ("/api/copilotkit/info", "/api/copilotkit"):
        missing = await client.get(path) if path.endswith("/info") else await client.post(path, json={})
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    stream = await client.post("/dataagent/stream", json={})
    assert stream.status_code == 422
    assert stream.status_code != 404
    sessions = await client.get("/sessions")
    assert sessions.status_code == 200
    assert sessions.json()["sessions"] == []


async def test_same_app_registers_control_plane_and_conversation_routes(client) -> None:
    health = await client.get("/healthz")
    body = health.json()
    assert body["success"] is True
    assert body["data"]["conversation"]["ready"] is True
    assert body["data"]["conversation"]["model"] == "test-model"
    assert isinstance(body["data"]["conversation"]["plugins"], list)
    me = await client.get("/api/v1/me")
    assert me.json()["success"] is True
    missing = await client.get("/api/v1/capabilities")
    assert missing.status_code == 404
    removed = await client.get("/api/copilotkit/info")
    assert removed.status_code == 404


async def test_cancel_unknown_run_is_not_success(client) -> None:
    empty = await client.post("/api/v1/runs/run-1/cancel")
    explicit = await client.post(
        "/api/v1/runs/run-1/cancel",
        content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert empty.status_code == 404
    assert empty.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert explicit.status_code == 404


async def test_engine_failure_keeps_the_control_plane_up() -> None:
    application = create_app(None)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as http:
            health = await http.get("/healthz")
            chat = await http.post("/dataagent/stream", json={"threadId": "t", "runId": "r", "messages": []})
            removed = await http.get("/api/copilotkit/info")
            me = await http.get("/api/v1/me")
            cancel = await http.post("/api/v1/runs/run-1/cancel")
            ready = await http.get("/ready")
    assert health.status_code == 200
    assert health.json()["data"]["conversation"]["ready"] is False
    assert ready.status_code == 503
    assert chat.status_code == 503
    assert chat.json()["error"]["code"] == "CONVERSATION_UNAVAILABLE"
    assert removed.status_code == 404
    assert removed.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert me.status_code == 200
    assert me.json()["data"]["user"]["id"] == "default"
    assert cancel.status_code == 404
    assert cancel.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_partial_startup_failure_does_not_leave_a_closed_session_index(
    runtime, monkeypatch,
) -> None:
    from contextlib import asynccontextmanager

    import serve.app as serve_app

    @asynccontextmanager
    async def failing(app, runtime, *, model=None):
        del runtime, model
        app.state.sessions = object()
        app.state.checkpointer = object()
        raise RuntimeError("checkpointer setup failed")
        yield

    monkeypatch.setattr(serve_app, "conversation_lifespan", failing)
    application = create_app(runtime)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as http:
            health = await http.get("/healthz")
            sessions = await http.get("/api/v1/sessions")
            native = await http.get("/sessions")
    assert health.json()["data"]["conversation"]["ready"] is False
    assert application.state.sessions is None
    assert application.state.checkpointer is None
    assert sessions.status_code == 503
    assert sessions.json()["error"]["code"] == "CONVERSATION_UNAVAILABLE"
    assert native.status_code == 503


async def test_cancel_stops_the_running_turn(client, app, monkeypatch) -> None:
    from ag_ui.core import RunErrorEvent

    started = asyncio.Event()
    stopped = asyncio.Event()
    calls = {"n": 0}

    async def hang(self, body):
        del self
        calls["n"] += 1
        if calls["n"] > 1:
            yield RunErrorEvent(message="second-run", code="TEST")
            return
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        yield RunErrorEvent(message="should-not-finish", code="TEST")

    monkeypatch.setattr("ag_ui_langgraph.LangGraphAgent.run", hang)
    body = {
        "threadId": "cancel-thread",
        "runId": "cancel-run",
        "state": {},
        "messages": [{"id": "m1", "role": "user", "content": "hello"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    running = asyncio.create_task(client.post("/dataagent/stream", json=body))
    await asyncio.wait_for(started.wait(), 5)
    user_id = app.state.agents.runtime.paths.user_id
    assert (user_id, "cancel-thread") in app.state.conversation_busy

    cancel = await client.post("/api/v1/runs/cancel-run/cancel")
    assert cancel.status_code == 204
    assert cancel.content == b""

    response = await asyncio.wait_for(running, 5)
    assert response.status_code == 200
    assert "RUN_FINISHED" not in response.text
    assert "should-not-finish" not in response.text
    await asyncio.wait_for(stopped.wait(), 5)
    assert (user_id, "cancel-thread") not in app.state.conversation_busy
    row = await app.state.sessions.get(user_id, "cancel-thread")
    assert row["status"] == "interrupted"

    follow = await client.post("/dataagent/stream", json={**body, "runId": "cancel-run-2"})
    assert follow.status_code == 200
    assert "RUN_FINISHED" not in follow.text
    assert "second-run" in follow.text
    assert (user_id, "cancel-thread") not in app.state.conversation_busy


def test_default_bind_is_loopback() -> None:
    settings = ServerSettings()
    assert settings.host == "127.0.0.1"
    assert settings.port == 8790


def test_public_bind_hosts_are_accepted() -> None:
    for host in ("0.0.0.0", "::", "192.168.1.10", "example.com", "[::]", "127.0.0.1", "localhost"):
        settings = ServerSettings(host=host, port=8790)
        assert settings.port == 8790
        assert settings.host == host.strip("[]")


def test_blank_host_is_rejected() -> None:
    for host in ("", "   ", "127.0.0.1/32"):
        with pytest.raises(ValueError, match="server.host"):
            ServerSettings(host=host)


def test_port_is_validated() -> None:
    with pytest.raises(ValueError):
        ServerSettings(port=0)
    with pytest.raises(ValueError):
        ServerSettings(port=70000)
