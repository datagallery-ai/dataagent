from __future__ import annotations

from conftest import register_and_login


def test_copilotkit_info_lists_datafoundry_agent(client) -> None:
    register_and_login(client)
    response = client.get("/api/copilotkit/info")
    assert response.status_code == 200
    body = response.json()
    assert body["agents"] == {}

    posted = client.post("/api/copilotkit", json={"method": "info"})
    assert posted.status_code == 200
    assert posted.json()["agents"] == {}


def test_copilotkit_rejects_single_endpoint_envelope(client) -> None:
    register_and_login(client)
    response = client.post(
        "/api/copilotkit",
        json={
            "method": "agent/run",
            "params": {"agentId": "dataFoundry"},
            "body": {
                "threadId": "thread-1",
                "runId": "run-1",
                "messages": [{"id": "m1", "role": "user", "content": "你好"}],
            },
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "BAD_REQUEST"


def test_copilotkit_requires_auth(client) -> None:
    response = client.post(
        "/api/copilotkit",
        json={"threadId": "thread-1", "runId": "run-1", "messages": []},
    )
    assert response.status_code == 401


def test_runtime_sqlite_files_are_initialized(client) -> None:
    settings = client.app.state.settings
    assert settings.checkpoint_db_path.exists()
    assert settings.store_db_path.exists()
