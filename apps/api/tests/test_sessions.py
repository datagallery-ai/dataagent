from __future__ import annotations


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
