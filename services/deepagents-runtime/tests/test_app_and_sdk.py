import json

from fastapi.testclient import TestClient

from deepagents import create_deep_agent
from deepagents_runtime.agent import create_runtime_agent
from deepagents_runtime.app import create_app
from deepagents_runtime.config import RuntimeSettings


def parse_sse(body: bytes) -> list[dict]:
    events = []
    for frame in body.decode("utf-8").split("\n\n"):
        line = frame.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                events.append(json.loads(payload))
    return events


def run_request(run_id: str, content: str, **extra) -> dict:
    return {
        "threadId": "session-sdk",
        "runId": run_id,
        "messages": [{"id": "m1", "role": "user", "content": content}],
        "systemPrompt": "test",
        **extra,
    }


def test_create_runtime_agent_uses_deepagents_sdk():
    agent = create_runtime_agent(RuntimeSettings(fake_model=True))
    assert hasattr(agent, "astream_events")
    assert hasattr(agent, "aget_state")
    assert create_deep_agent.__module__.startswith("deepagents")


def test_health_and_text_stream_through_sdk():
    client = TestClient(create_app(RuntimeSettings(fake_model=True)))
    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["provider"] == "deepagents"
    assert body["capabilities"]["interrupt"] is True

    response = client.post("/runs/stream", json=run_request("r-text", "你好"))
    assert response.status_code == 200
    events = parse_sse(response.content)
    types = [event["type"] for event in events]
    assert "RUN_STARTED" in types
    assert "TEXT_MESSAGE_CONTENT" in types
    assert "RUN_FINISHED" in types
    assert any(event.get("name") == "runtime.bound" for event in events)
    text = "".join(event.get("delta", "") for event in events if event["type"] == "TEXT_MESSAGE_CONTENT")
    assert "Deep Agents SDK" in text or "你好" in text


def test_interrupt_and_resume_through_sdk():
    client = TestClient(create_app(RuntimeSettings(fake_model=True)))
    interrupted = client.post("/runs/stream", json=run_request("r-ask", "please interrupt"))
    events = parse_sse(interrupted.content)
    interrupt = next(event for event in events if event.get("name") == "on_interrupt")
    assert interrupt["value"]["type"] == "agent_interrupt"
    assert interrupt["value"]["toolName"] == "ask_user"
    assert "RUN_FINISHED" not in [event["type"] for event in events]

    resumed = client.post("/runs/stream", json=run_request(
        "r-ask",
        "please interrupt",
        resume={"interrupt": interrupt["value"], "response": {"answer": "继续"}},
    ))
    resume_events = parse_sse(resumed.content)
    assert any(event["type"] == "RUN_FINISHED" for event in resume_events)
    assert any(
        event["type"] in {"TOOL_CALL_RESULT", "TEXT_MESSAGE_CONTENT"}
        for event in resume_events
    )


def test_write_todos_tool_goes_through_sdk():
    client = TestClient(create_app(RuntimeSettings(fake_model=True)))
    response = client.post("/runs/stream", json=run_request("r-tool", "make a plan"))
    events = parse_sse(response.content)
    assert any(event.get("toolCallName") == "write_todos" for event in events)
    assert any(event["type"] == "TOOL_CALL_RESULT" for event in events)
    assert any(event["type"] == "RUN_FINISHED" for event in events)


def test_cancel_unknown_run_is_ok():
    client = TestClient(create_app(RuntimeSettings(fake_model=True)))
    response = client.post("/runs/missing/cancel", json={"reason": "RUN_CANCELLED"})
    assert response.status_code == 200
    assert response.json()["canceled"] is False
