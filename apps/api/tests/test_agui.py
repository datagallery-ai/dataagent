from __future__ import annotations

import asyncio

from ag_ui.core import RunErrorEvent


async def test_control_plane_serves_gui_chat_on_the_agentkit_routes(client) -> None:
    info = await client.get("/api/copilotkit/info")
    assert info.status_code == 200
    assert info.json() == {"version": "1.0.0", "agents": {}}

    posted = await client.post("/api/copilotkit", json={})
    assert posted.status_code == 400
    assert posted.status_code != 404

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
    info = await client.get("/api/copilotkit/info")
    assert info.status_code != 404


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


async def test_engine_failure_keeps_the_control_plane_up(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("API_HOST", "127.0.0.1")
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DATAAGENT_CONFIG", str(tmp_path / "missing.json"))
    from datafoundry_api.app import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        transport_app = application
        import httpx

        transport = httpx.ASGITransport(app=transport_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as http:
            health = await http.get("/healthz")
            chat = await http.post("/api/copilotkit", json={"threadId": "t", "runId": "r", "messages": []})
            me = await http.get("/api/v1/me")
            cancel = await http.post("/api/v1/runs/run-1/cancel")
            ready = await http.get("/ready")
    assert health.status_code == 200
    assert health.json()["data"]["conversation"]["ready"] is False
    assert ready.status_code == 503
    assert chat.status_code == 503
    assert chat.json()["error"]["code"] == "CONVERSATION_UNAVAILABLE"
    assert me.status_code == 200
    assert me.json()["data"]["user"]["id"] == "default"
    assert cancel.status_code == 404
    assert cancel.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_cancel_stops_the_running_turn(client, app, monkeypatch) -> None:
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
        "messages": [{"id": "m1", "role": "user", "content": "hello"}],
    }
    running = asyncio.create_task(client.post("/api/copilotkit", json=body))
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

    follow = await client.post("/api/copilotkit", json={**body, "runId": "cancel-run-2"})
    assert follow.status_code == 200
    assert "RUN_FINISHED" not in follow.text
    assert "second-run" in follow.text
    assert (user_id, "cancel-thread") not in app.state.conversation_busy
