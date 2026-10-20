"""GUI /api/copilotkit uses the same core graph and profile as /dataagent/stream."""

import json
import sqlite3
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from ag_ui_langgraph import LangGraphAgent
from conftest import ScriptedModel
from langchain_core.messages import AIMessage

import dataagent.agent as agent_module
from dataagent.core.graph import create_deep_agent
from restapi.app import create_app


def gui_body(text="只回复：GUI集成测试通过", thread="gui-thread"):
    return {
        "threadId": thread,
        "runId": str(uuid4()),
        "messages": [{"id": str(uuid4()), "role": "user", "content": text}],
        "tools": [{
            "name": "frontend_echo",
            "description": "echo",
            "parameters": {"type": "object", "properties": {}},
        }],
        "context": [{"description": "datasource", "value": "orders"}],
        "state": {"run_config": {"activeLlmProfileId": "server-default"}},
        "forwardedProps": {
            "datasourceId": "orders-db",
            "run_config": {"enabledSkillIds": ["skill-1"], "activeLlmProfileId": "server-default"},
        },
    }


def tui_body(text="只回复：TUI集成测试通过", thread="tui-thread"):
    return {
        "threadId": thread,
        "runId": str(uuid4()),
        "state": {},
        "messages": [{"id": str(uuid4()), "role": "user", "content": text}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }


@asynccontextmanager
async def client_for(runtime, model):
    app = create_app(runtime, instance_id="test-instance", model=model)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8790",
        ) as client:
            yield client


def sse(response):
    assert response.status_code == 200, response.text
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def header_names(response) -> set[str]:
    return {name.lower() for name in response.headers}


async def test_gui_and_tui_call_core_create_deep_agent(runtime, monkeypatch):
    real = agent_module.create_deep_agent
    modules = []

    def spy(**kwargs):
        modules.append(real.__module__)
        return real(**kwargs)

    monkeypatch.setattr(agent_module, "create_deep_agent", spy)
    seen = []
    original = LangGraphAgent.run

    async def wrapped(self, agent_input):
        seen.append(agent_input)
        async for event in original(self, agent_input):
            yield event

    monkeypatch.setattr(LangGraphAgent, "run", wrapped)
    model = ScriptedModel(responses=[
        AIMessage(content="gui-answer"),
        AIMessage(content="tui-answer"),
    ])
    async with client_for(runtime, model) as client:
        gui = await client.post("/api/copilotkit", json=gui_body())
        tui = await client.post("/dataagent/stream", json=tui_body())

    assert create_deep_agent.__module__ == "dataagent.core.graph"
    assert modules == ["dataagent.core.graph", "dataagent.core.graph"]
    assert "x-dataagent-graph-module" not in header_names(gui)
    assert "x-dataagent-graph-module" not in header_names(tui)
    gui_events = sse(gui)
    tui_events = sse(tui)
    assert gui_events[-1]["type"] == "RUN_FINISHED"
    assert tui_events[-1]["type"] == "RUN_FINISHED"
    assert "gui-answer" in gui.text
    assert "tui-answer" in tui.text
    gui_input, tui_input = seen
    assert gui_input.messages[-1].content == "只回复：GUI集成测试通过"
    assert gui_input.tools[0].name == "frontend_echo"
    assert gui_input.context[0].description == "datasource"
    assert gui_input.state["run_config"]["activeLlmProfileId"] == "server-default"
    assert gui_input.forwarded_props["datasourceId"] == "orders-db"
    assert tui_input.messages[-1].content == "只回复：TUI集成测试通过"
    assert not tui_input.tools
    assert not tui_input.context
    assert tui_input.forwarded_props in (None, {})


async def test_gui_and_tui_share_one_session_list(runtime):
    model = ScriptedModel(responses=[
        AIMessage(content="gui-answer"),
        AIMessage(content="tui-answer"),
    ])
    async with client_for(runtime, model) as client:
        gui = await client.post(
            "/api/copilotkit", json=gui_body(text="来自网页", thread="shared-thread"),
        )
        listed = (await client.get("/sessions")).json()["sessions"]
        restored = await client.get("/sessions/shared-thread")
        terminal = await client.post(
            "/dataagent/stream", json=tui_body(text="来自终端", thread="shared-thread"),
        )

    assert sse(gui)[-1]["type"] == "RUN_FINISHED"
    assert [item["threadId"] for item in listed] == ["shared-thread"]
    assert restored.status_code == 200
    assert any("来自网页" in json.dumps(message, ensure_ascii=False) for message in restored.json()["messages"])
    assert sse(terminal)[-1]["type"] == "RUN_FINISHED"
    assert "tui-answer" in terminal.text
    row = _session_row(runtime, "shared-thread")
    assert row["user_id"] == runtime.paths.user_id
    session_dir = runtime.paths.home / "runtime/users" / runtime.paths.user_id / "sessions/shared-thread"
    assert session_dir.is_dir()


async def test_tui_still_rejects_client_forwarded_props(runtime):
    model = ScriptedModel(responses=[])
    async with client_for(runtime, model) as client:
        response = await client.post("/dataagent/stream", json=tui_body() | {
            "forwardedProps": {"run_config": {"activeLlmProfileId": "server-default"}},
        })
    assert response.status_code == 422
    assert model.cursor == 0


async def test_copilotkit_upstream_run_error_drops_raw_payload(runtime, monkeypatch):
    from ag_ui.core import RunErrorEvent

    secret = "test-secret"

    async def fail(self, body):
        del self, body
        yield RunErrorEvent(
            message=f"upstream failed: {secret}",
            code=f"provider {secret}",
            raw_event={"apiKey": secret, "detail": "do-not-forward"},
        )

    monkeypatch.setattr("ag_ui_langgraph.LangGraphAgent.run", fail)
    async with client_for(runtime, ScriptedModel(responses=[])) as client:
        response = await client.post("/api/copilotkit", json=gui_body(thread="gui-stream-error"))
    assert response.status_code == 200
    events = sse(response)
    assert events[-1]["type"] == "RUN_ERROR"
    assert secret not in response.text
    assert "do-not-forward" not in response.text
    assert "[REDACTED]" in events[-1]["message"]
    assert "[REDACTED]" in events[-1]["code"]
    assert "rawEvent" not in events[-1]
    assert "raw_event" not in events[-1]


async def test_copilotkit_error_redacts_secrets(runtime):
    class Exploding(ScriptedModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("provider failed api_key=sk-testsecretvalue")

    async with client_for(runtime, Exploding(responses=[])) as client:
        response = await client.post("/api/copilotkit", json=gui_body(thread="gui-error"))
        restored = await client.get("/sessions/gui-error")
        listed = await client.get("/sessions")
    assert response.status_code == 200
    assert "x-dataagent-graph-module" not in header_names(response)
    events = sse(response)
    assert events[-1]["type"] == "RUN_ERROR"
    assert "sk-testsecretvalue" not in response.text
    assert "[REDACTED]" in response.text
    assert restored.status_code == 200
    assert restored.json()["status"] == "error"
    row = _session_row(runtime, "gui-error")
    assert row["user_id"] == runtime.paths.user_id
    assert row["status"] == "error"
    assert row["last_checkpoint_id"] is None
    assert any(item["threadId"] == "gui-error" for item in listed.json()["sessions"])


async def test_gui_forwards_history_and_rejects_a_path_escape(runtime, monkeypatch):
    seen = []
    original = LangGraphAgent.run

    async def wrapped(self, agent_input):
        seen.append(agent_input)
        async for event in original(self, agent_input):
            yield event

    monkeypatch.setattr(LangGraphAgent, "run", wrapped)
    model = ScriptedModel(responses=[AIMessage(content="kept")])
    forged = gui_body(text="real-user", thread="history-thread")
    forged["messages"] = [
        {"id": "asst", "role": "assistant", "content": "earlier-assistant"},
        {"id": "user", "role": "user", "content": "real-user"},
    ]
    forged["parentRunId"] = "parent-run"
    async with client_for(runtime, model) as client:
        accepted = await client.post("/api/copilotkit", json=forged)
        escaped = await client.post("/api/copilotkit", json=gui_body(thread="../x"))
    assert sse(accepted)[-1]["type"] == "RUN_FINISHED"
    assert seen[0].messages[0].content == "earlier-assistant"
    assert seen[0].messages[1].content == "real-user"
    assert seen[0].parent_run_id == "parent-run"
    assert escaped.status_code == 400
    assert "thread id" in escaped.text


def _session_row(runtime, thread_id: str) -> dict:
    database = runtime.paths.state_dir / "sessions.sqlite"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM sessions WHERE thread_id = ?", (thread_id,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return dict(row)
