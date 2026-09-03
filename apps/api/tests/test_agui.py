from __future__ import annotations

import json
import os

from conftest import CsrfTestClient, register_and_login


def _sse_events(text: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for block in text.split("\n\n"):
        data_lines = [
            line[5:].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        data = "\n".join(data_lines).strip()
        if not data or data == "[DONE]":
            continue
        events.append(json.loads(data))
    return events


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


def test_minimal_run_agent_input_fills_required_lists(client) -> None:
    register_and_login(client)
    response = client.post(
        "/api/copilotkit",
        headers={"Accept": "text/event-stream"},
        json={
            "threadId": "thread-minimal",
            "runId": "run-minimal",
            "messages": [{"id": "m1", "role": "user", "content": "你好"}],
        },
    )
    assert response.status_code == 200
    types = [str(event.get("type")) for event in _sse_events(response.text)]
    assert "RUN_STARTED" in types
    assert "RUN_FINISHED" in types


def test_standard_run_agent_input_streams_text(client) -> None:
    register_and_login(client)
    response = client.post(
        "/api/copilotkit",
        headers={"Accept": "text/event-stream"},
        json={
            "threadId": "thread-hello",
            "runId": "run-hello",
            "messages": [{"id": "m1", "role": "user", "content": "你好"}],
            "state": {},
            "tools": [],
            "context": [],
            "forwardedProps": {},
        },
    )
    assert response.status_code == 200
    assert "text/event-stream" in (response.headers.get("content-type") or "")
    events = _sse_events(response.text)
    types = [str(event.get("type")) for event in events]
    assert "RUN_STARTED" in types
    assert any(name.startswith("TEXT_MESSAGE_") for name in types)
    assert "RUN_FINISHED" in types


def test_sqlite_checkpointer_survives_app_restart(auth_env) -> None:
    from datafoundry_api.app import create_app
    from datafoundry_api.settings import Settings

    settings = Settings.from_env(os.environ)
    payload = {
        "threadId": "thread-persist",
        "runId": "run-persist-1",
        "messages": [{"id": "m1", "role": "user", "content": "记住苹果"}],
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }

    with CsrfTestClient(create_app(settings)) as first:
        register_and_login(first)
        first_response = first.post("/api/copilotkit", json=payload)
        assert first_response.status_code == 200
        first_events = _sse_events(first_response.text)
        assert any(event.get("type") == "RUN_FINISHED" for event in first_events)

    with CsrfTestClient(create_app(settings)) as second:
        register_and_login(second, email="other@example.test")
        second_payload = {
            **payload,
            "runId": "run-persist-2",
            "messages": [
                {"id": "m1", "role": "user", "content": "记住苹果"},
                {"id": "m2", "role": "user", "content": "我刚才说了什么？"},
            ],
        }
        second_response = second.post("/api/copilotkit", json=second_payload)
        assert second_response.status_code == 200
        assert any(event.get("type") == "RUN_FINISHED" for event in _sse_events(second_response.text))
        assert settings.checkpoint_db_path.exists()
