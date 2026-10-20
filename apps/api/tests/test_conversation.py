from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "conversation-test"

    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        del messages, stop, run_manager, kwargs
        message = self.responses[self.cursor].model_copy(deep=True, update={"id": str(uuid4())})
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop, run_manager, **kwargs)


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
    return [item["contentText"] for item in payload["messages"]]


async def test_missing_conversation_is_empty(client) -> None:
    response = await client.get("/api/v1/sessions/missing-thread/conversation")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["sessionId"] == "missing-thread"
    assert data["messages"] == []
    assert data["agentMessages"] == []


async def test_web_and_tui_writes_are_readable(api_env, monkeypatch) -> None:
    del api_env
    import restapi.app as restapi_app

    scripted = ScriptedModel(responses=[
        AIMessage(content="from-web"),
        AIMessage(content="from-tui"),
    ])
    real_lifespan = restapi_app.conversation_lifespan

    @asynccontextmanager
    async def lifespan(app, runtime, *, model=None):
        del model
        async with real_lifespan(app, runtime, model=scripted):
            yield

    monkeypatch.setattr(restapi_app, "conversation_lifespan", lifespan)
    from datafoundry_api.app import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as http:
            web = await http.post("/api/copilotkit", json=_gui("来自网页", "web-thread"))
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
    assert web_body["sessionId"] == "web-thread"
    assert web_body["toolCalls"] == []
    assert "来自网页" in _texts(web_body)
    assert "from-web" in _texts(web_body)
    assert "来自终端" in _texts(tui_body)
    assert "from-tui" in _texts(tui_body)
    assert running.status_code == 200
    assert "来自网页" in _texts(running.json()["data"])


async def test_agent_messages_keep_tool_calls_in_checkpoint_order(api_env, monkeypatch) -> None:
    del api_env
    import restapi.app as restapi_app

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
    real_lifespan = restapi_app.conversation_lifespan

    @asynccontextmanager
    async def lifespan(app, runtime, *, model=None):
        del model
        async with real_lifespan(app, runtime, model=scripted):
            yield

    monkeypatch.setattr(restapi_app, "conversation_lifespan", lifespan)
    from datafoundry_api.app import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as http:
            posted = await http.post("/api/copilotkit", json=_gui("tool-1", "tool-thread"))
            view = await http.get("/api/v1/sessions/tool-thread/conversation")
            native = await http.get("/sessions/tool-thread")
    assert posted.status_code == 200, posted.text
    assert view.status_code == 200, view.text
    data = view.json()["data"]
    roles = [message.get("role") for message in data["agentMessages"]]
    assert "tool" in roles
    assert any(message.get("toolCalls") or message.get("tool_calls") for message in data["agentMessages"])
    assert data["agentMessages"] == native.json()["messages"]
