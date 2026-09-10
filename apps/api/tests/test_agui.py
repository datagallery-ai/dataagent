from __future__ import annotations

import asyncio
import json

import pytest
from ag_ui.core.events import RunErrorEvent, RunFinishedEvent, RunStartedEvent, TextMessageContentEvent
from conftest import register_and_login
from langgraph.errors import GraphRecursionError

from datafoundry_api.model_profiles import RuntimeModelSelection


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


@pytest.mark.parametrize("timeout_ms", [None, 1000])
@pytest.mark.parametrize("kind,code", [
    ("recursion", "GRAPH_RECURSION_LIMIT"),
    ("unexpected", "RUN_EXECUTION_ERROR"),
    ("timeout", "RUN_TIMEOUT"),
    ("eof", "RUN_INCOMPLETE"),
])
def test_stream_failures_preserve_partial_output_and_emit_one_safe_error(client, monkeypatch, timeout_ms, kind, code):
    register_and_login(client)
    closed = []

    class Agent:
        async def run(self, input):
            try:
                yield RunStartedEvent(thread_id=input.thread_id, run_id=input.run_id)
                yield TextMessageContentEvent(message_id="answer", delta="partial answer")
                if kind == "recursion":
                    raise GraphRecursionError("Recursion limit of 25 reached; secret-fixture-value")
                if kind == "unexpected":
                    raise RuntimeError("api_key=secret-fixture-value /private/path")
                if kind == "timeout":
                    raise TimeoutError("upstream secret-fixture-value")
            finally:
                closed.append(True)

    async def agent_for(*args, **kwargs):
        return Agent()

    monkeypatch.setattr(client.app.state.agent_runtime, "agent_for", agent_for)
    monkeypatch.setattr(client.app.state.model_profiles, "resolve_model_selection", lambda *args: RuntimeModelSelection(
        cache_key="test", model_slots=None, run_timeout_ms=timeout_ms,
    ))
    response = client.post("/api/copilotkit", json={"threadId": "thread-1", "runId": "run-1", "messages": []})
    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert [event["type"] for event in events] == ["RUN_STARTED", "TEXT_MESSAGE_CONTENT", "RUN_ERROR"]
    assert events[1]["delta"] == "partial answer"
    assert events[-1]["code"] == code
    assert "secret-fixture-value" not in response.text
    assert "/private/path" not in response.text
    assert "Traceback" not in response.text
    assert closed == [True]


@pytest.mark.parametrize("kind", ["deadline", "before_start", "finished", "error"])
def test_stream_terminal_and_deadline_handling(client, monkeypatch, kind):
    register_and_login(client)
    closed = []

    class Agent:
        async def run(self, input):
            try:
                if kind == "deadline":
                    await asyncio.sleep(10)
                if kind == "finished":
                    yield RunFinishedEvent(thread_id=input.thread_id, run_id=input.run_id)
                elif kind == "error":
                    yield RunErrorEvent(code="TOOL_FAILED", message="Tool failed")
                raise RuntimeError("internal detail")
            finally:
                closed.append(True)

    async def agent_for(*args, **kwargs):
        return Agent()

    monkeypatch.setattr(client.app.state.agent_runtime, "agent_for", agent_for)
    monkeypatch.setattr(client.app.state.model_profiles, "resolve_model_selection", lambda *args: RuntimeModelSelection(
        cache_key="test", model_slots=None, run_timeout_ms=10 if kind == "deadline" else None,
    ))
    response = client.post("/api/copilotkit", json={"threadId": "thread-1", "runId": "run-1", "messages": []})
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert len(events) == 1
    assert events[0]["type"] == ("RUN_FINISHED" if kind == "finished" else "RUN_ERROR")
    if kind != "finished":
        assert events[0]["code"] == {"deadline": "RUN_TIMEOUT", "before_start": "RUN_EXECUTION_ERROR", "error": "TOOL_FAILED"}[kind]
    if kind == "deadline":
        assert "10 ms" in events[0]["message"]
    assert closed == [True]
