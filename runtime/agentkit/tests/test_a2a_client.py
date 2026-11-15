from contextlib import asynccontextmanager

import httpx
import pytest
from conftest import ScriptedModel
from langchain_core.messages import AIMessage

from a2a_server import A2ACaller, A2ACallError, create_a2a_server
from a2a_server.client import remote_agent_tool


@asynccontextmanager
async def serving(runtime, model, **kwargs):
    app = create_a2a_server(runtime, host="127.0.0.1", port=9999, model=model, **kwargs)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9999") as http:
            yield http


async def test_caller_round_trip_keeps_context(runtime):
    model = ScriptedModel(responses=[
        AIMessage(content="first"),
        AIMessage(content="second"),
    ])
    async with serving(runtime, model) as http:
        caller = A2ACaller("http://127.0.0.1:9999", http_client=http)
        first = await caller.send("one")
        second = await caller.send("two")
    assert first.text == "first"
    assert first.state == "TASK_STATE_COMPLETED"
    assert second.text == "second"
    assert second.context_id == first.context_id == caller.context_id
    humans = [
        item.content for item in model.requests[1] if getattr(item, "type", None) == "human"
    ]
    assert humans == ["one", "two"]


async def test_failed_task_raises(runtime):
    async with serving(runtime, ScriptedModel(responses=[])) as http:
        caller = A2ACaller("http://127.0.0.1:9999", http_client=http)
        with pytest.raises(A2ACallError, match="ran out of responses"):
            await caller.send("hello")


async def test_blank_message_is_rejected_locally():
    caller = A2ACaller("http://127.0.0.1:9999")
    with pytest.raises(ValueError, match="must not be blank"):
        await caller.send("   ")


def test_agent_id_must_be_a_tool_name():
    with pytest.raises(ValueError, match="agent id"):
        remote_agent_tool("bad id", "http://127.0.0.1:9999")


async def test_remote_tool_keeps_one_context_per_local_thread(runtime):
    model = ScriptedModel(responses=[
        AIMessage(content="a1"),
        AIMessage(content="a2"),
        AIMessage(content="b1"),
    ])
    async with serving(runtime, model) as http:
        tool = remote_agent_tool("researcher", "http://127.0.0.1:9999", http_client=http)
        assert "config" not in tool.args
        await tool.ainvoke(
            {"message": "one"}, config={"configurable": {"thread_id": "local-a"}},
        )
        await tool.ainvoke(
            {"message": "two"}, config={"configurable": {"thread_id": "local-a"}},
        )
        await tool.ainvoke(
            {"message": "three"}, config={"configurable": {"thread_id": "local-b"}},
        )

    def humans(index):
        return [
            item.content for item in model.requests[index] if getattr(item, "type", None) == "human"
        ]

    assert humans(1) == ["one", "two"]
    assert humans(2) == ["three"]


def test_configured_agents_compile_into_the_graph(runtime):
    from dataclasses import replace

    from pydantic import SecretStr, ValidationError

    from dataagent.agent import load_extensions
    from dataagent.settings import A2AAgentSettings, A2ASettings

    with pytest.raises(ValidationError, match="duplicate ids"):
        A2ASettings(agents=(
            A2AAgentSettings(id="researcher", url="http://127.0.0.1:9999"),
            A2AAgentSettings(id="researcher", url="http://127.0.0.1:9998"),
        ))
    settings = runtime.settings.model_copy(update={"a2a": A2ASettings(agents=(
        A2AAgentSettings(
            id="researcher",
            url="http://127.0.0.1:9999",
            description="Ask the research agent.",
            auth_token=SecretStr("a2a-token"),
        ),
    ))})
    prepared = replace(runtime, settings=settings)
    names = [tool.name for tool in load_extensions(prepared)["tools"]]
    assert "a2a__researcher" in names
    assert "a2a-token" in prepared.redaction_secrets


@asynccontextmanager
async def peer(respond):
    """A JSON-RPC peer that returns the protobuf ``SendMessageResponse`` from ``respond``."""
    from a2a.server.request_handlers.response_helpers import agent_card_to_dict
    from google.protobuf.json_format import MessageToDict
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from a2a_server.agent_card import build_agent_card

    card = agent_card_to_dict(build_agent_card(host="127.0.0.1", port=9999))

    async def get_card(_request):
        return JSONResponse(card)

    async def rpc(request):
        body = await request.json()
        message = body["params"]["message"]
        result = MessageToDict(respond(message))
        return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "result": result})

    app = Starlette(routes=[
        Route("/.well-known/agent-card.json", get_card),
        Route("/a2a/jsonrpc", rpc, methods=["POST"]),
    ])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:9999") as http:
        yield http


async def test_direct_message_response_keeps_the_context():
    from a2a.helpers import new_text_message
    from a2a.types.a2a_pb2 import Role, SendMessageResponse

    def respond(_message):
        return SendMessageResponse(message=new_text_message(
            "plain-answer", context_id="ctx-msg", role=Role.ROLE_AGENT,
        ))

    async with peer(respond) as http:
        result = await A2ACaller("http://127.0.0.1:9999", http_client=http).send("hello")
    assert result.text == "plain-answer"
    assert result.context_id == "ctx-msg"
    assert result.state == "MESSAGE"


async def test_input_required_follow_up_keeps_the_task():
    from a2a.helpers import new_text_artifact, new_text_message
    from a2a.types.a2a_pb2 import SendMessageResponse, Task, TaskState, TaskStatus

    seen = []

    def respond(message):
        seen.append(message.get("taskId") or message.get("task_id"))
        if len(seen) == 1:
            return SendMessageResponse(task=Task(
                id="task-wait",
                context_id="ctx-1",
                status=TaskStatus(
                    state=TaskState.TASK_STATE_INPUT_REQUIRED,
                    message=new_text_message("need more", context_id="ctx-1", task_id="task-wait"),
                ),
            ))
        return SendMessageResponse(task=Task(
            id="task-wait",
            context_id="ctx-1",
            status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            artifacts=[new_text_artifact("agentkit_result", "done")],
        ))

    async with peer(respond) as http:
        tool = remote_agent_tool("researcher", "http://127.0.0.1:9999", http_client=http)
        waiting = await tool.ainvoke(
            {"message": "start"}, config={"configurable": {"thread_id": "local-1"}},
        )
        finished = await tool.ainvoke(
            {"message": "more"}, config={"configurable": {"thread_id": "local-1"}},
        )
    assert waiting == "INPUT_REQUIRED\nneed more"
    assert seen == [None, "task-wait"]
    assert finished == "done"
