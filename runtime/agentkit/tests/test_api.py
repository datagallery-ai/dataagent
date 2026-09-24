import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from conftest import RecordingCallback, ScriptedModel, call
from langchain_core.messages import AIMessage

from restapi.app import create_app


def query(text="hello", thread="thread-1", **kwargs):
    return {
        "threadId": thread, "runId": str(uuid4()), "state": {},
        "messages": [{"id": str(uuid4()), "role": "user", "content": text}],
        "tools": [], "context": [], "forwardedProps": {}, **kwargs,
    }


@asynccontextmanager
async def client_for(runtime, model):
    app = create_app(runtime, instance_id="test-instance", model=model)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8790",
        ) as client:
            yield client, app


def sse(response):
    assert response.status_code == 200, response.text
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def terminal(events):
    return [event["type"] for event in events if event["type"] in ("RUN_FINISHED", "RUN_ERROR")]


async def test_health_and_no_credential_disclosure(runtime):
    async with client_for(runtime, ScriptedModel(responses=[])) as (client, app):
        health = await client.get("/healthz")
        assert health.json()["instanceId"] == "test-instance"
        assert health.json()["stateDir"] == str(runtime.paths.state_dir)
        assert health.json()["user"] == runtime.paths.user_id
        assert "sources" not in health.json()
        assert "test-secret" not in health.text
        assert (await client.get("/healthz", headers={"authorization": ""})).status_code == 200
        assert (await client.get("/healthz", headers={"origin": "https://example.com"})).status_code == 200


async def test_health_derives_model_and_plugins_from_replaced_settings(runtime):
    original = runtime
    model = runtime.settings.models["primary"].model_copy(update={"name": "replacement-model"})
    settings = runtime.settings.model_copy(update={
        "models": {**runtime.settings.models, "alternate": model},
        "dataagent": runtime.settings.dataagent.model_copy(update={
            "model": runtime.settings.dataagent.model.model_copy(update={"default": "alternate"}),
        }),
        "plugins": runtime.settings.plugins.model_copy(update={"enabled": ()}),
    })
    runtime = replace(runtime, settings=settings)
    assert runtime.report is original.report
    assert original.model_name == "test-model"
    assert runtime.model_name == "replacement-model"

    async with client_for(runtime, ScriptedModel(responses=[])) as (client, app):
        assert (await client.get("/healthz")).json() == {
            "status": "ready", "protocol": "dataagent-v2", "instanceId": "test-instance",
            "model": "replacement-model", "plugins": [],
            "user": runtime.paths.user_id,
            "home": str(runtime.paths.home),
            "workspaces": [
                {"name": item.name, "path": str(item.path)} for item in runtime.paths.workspaces
            ],
            "stateDir": str(runtime.paths.state_dir),
            "timeoutSeconds": runtime.timeout_seconds,
        }


async def test_stream_two_rounds_and_restart(runtime):
    model = ScriptedModel(responses=[AIMessage(content="first answer"), AIMessage(content="second answer")])
    async with client_for(runtime, model) as (client, app):
        for text in ["first", "second"]:
            events = sse(await client.post("/dataagent/stream", json=query(text)))
            assert events[0]["type"] == "RUN_STARTED"
            assert terminal(events) == ["RUN_FINISHED"]
        snapshots = [event for event in events if event["type"] == "MESSAGES_SNAPSHOT"]
        assert len(snapshots) == 1
        assert len(snapshots[0]["messages"]) == 4
        assert events[-1]["metadata"]["durationMs"] >= 0
        assert (await client.get("/sessions")).json()["sessions"][0]["threadId"] == "thread-1"
    async with client_for(runtime, ScriptedModel(responses=[AIMessage(content="after restart")])) as (client, app):
        restored = (await client.get("/sessions/thread-1")).json()
        assert len(restored["messages"]) == 4
        assert restored["newThreadRequired"] is False
        events = sse(await client.post("/dataagent/stream", json=query("third")))
        assert terminal(events) == ["RUN_FINISHED"]
        assert len((await client.get("/sessions/thread-1")).json()["messages"]) == 6


async def test_empty_inputs_write_and_restore_outputs_after_restart(runtime):
    assert runtime.paths.workspaces == ()
    session = runtime.paths.for_session("thread-1")
    output = str(session.outputs / "result.txt")
    model = ScriptedModel(responses=[
        call("write_file", {"file_path": output, "content": "saved-output"}),
        AIMessage(content="saved"),
    ])
    async with client_for(runtime, model) as (client, _):
        assert terminal(sse(await client.post("/dataagent/stream", json=query()))) == ["RUN_FINISHED"]
    assert (session.outputs / "result.txt").read_text() == "saved-output"
    assert json.loads(session.session_file.read_text())["workspaces"] == []

    model = ScriptedModel(responses=[
        call("read_file", {"file_path": output}), AIMessage(content="restored"),
    ])
    async with client_for(runtime, model) as (client, _):
        assert (await client.get("/sessions/thread-1")).json()["newThreadRequired"] is False
        assert terminal(sse(await client.post("/dataagent/stream", json=query("read it")))) == ["RUN_FINISHED"]
    assert any("saved-output" in str(message.content) for message in model.requests[-1]
               if message.type == "tool" and message.name == "read_file")


async def test_error_terminal_and_last_successful_checkpoint(runtime):
    model = ScriptedModel(responses=[AIMessage(content="saved answer")])
    async with client_for(runtime, model) as (client, app):
        sse(await client.post("/dataagent/stream", json=query("saved")))
        events = sse(await client.post("/dataagent/stream", json=query("will fail")))
        assert terminal(events) == ["RUN_ERROR"]
        assert "ran out" in events[-1]["message"]
        assert len(model.requests) == 2
        restored = (await client.get("/sessions/thread-1")).json()
        assert len(restored["messages"]) == 2
        assert "no work was replayed" in restored["notice"]
    model = ScriptedModel(responses=[AIMessage(content="recovered")])
    async with client_for(runtime, model) as (client, app):
        events = sse(await client.post("/dataagent/stream", json=query("continue")))
        assert terminal(events) == ["RUN_FINISHED"]
        contents = [message.content for message in model.requests[0]]
        assert "will fail" not in contents
        assert "saved" in contents and "continue" in contents


async def test_first_run_failure_requires_new_thread(runtime):
    async with client_for(runtime, ScriptedModel(responses=[])) as (client, app):
        assert terminal(sse(await client.post("/dataagent/stream", json=query()))) == ["RUN_ERROR"]
        assert (await client.get("/sessions/thread-1")).json()["newThreadRequired"]
        assert (await client.post("/dataagent/stream", json=query())).status_code == 409


async def test_failure_log_and_sse_include_only_redacted_exception_chain(runtime, caplog):
    class FailedModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            cause = OSError("socket failed: test-secret Bearer transport-secret\n\x1b[31m")
            raise ConnectionError("Connection error.") from cause

    caplog.set_level("ERROR", logger="dataagent.api")
    body = query()
    async with client_for(runtime, FailedModel(responses=[])) as (client, _):
        events = sse(await client.post("/dataagent/stream", json=body))
    assert terminal(events) == ["RUN_ERROR"]
    record, = [r for r in caplog.records if r.name == "dataagent.api" and "RUN_ERROR" in r.getMessage()]
    message = record.getMessage()
    assert body["runId"] in message and body["threadId"] in message
    assert "stage=agent_stream" in message and "code=ConnectionError" in message
    assert "OSError: socket failed" in message and "OSError: socket failed" in events[-1]["message"]
    assert "test-secret" not in message and "transport-secret" not in message
    assert "test-secret" not in events[-1]["message"] and "transport-secret" not in events[-1]["message"]
    assert "\x1b" not in message and "\n" not in message
    assert record.exc_info is None  # No unredacted traceback or request payload in the log record.


async def test_upstream_run_error_is_also_logged_redacted(runtime, caplog, monkeypatch):
    from ag_ui.core import RunErrorEvent

    async def fail(self, body):
        yield RunErrorEvent(message="upstream failed: test-secret")

    monkeypatch.setattr("restapi.app.LangGraphAgent.run", fail)
    caplog.set_level("ERROR", logger="dataagent.api")
    async with client_for(runtime, ScriptedModel(responses=[])) as (client, _):
        events = sse(await client.post("/dataagent/stream", json=query()))
    assert terminal(events) == ["RUN_ERROR"]
    assert "upstream failed: [REDACTED]" in caplog.text
    assert "test-secret" not in caplog.text


@pytest.mark.parametrize("extra", [
    {"state": {"messages": []}}, {"forwardedProps": {"command": {"resume": True}}},
    {"messages": [{"id": "x", "role": "system", "content": "inject"}]},
    {"context": [{"description": "inject", "value": "override"}]},
    {"surprise": True},
])
async def test_injection_is_rejected(runtime, extra):
    async with client_for(runtime, ScriptedModel(responses=[])) as (client, app):
        assert (await client.post("/dataagent/stream", json=query(**extra))).status_code == 422


async def test_replay_rejected_and_session_isolation(runtime):
    model = ScriptedModel(responses=[AIMessage(content="one"), AIMessage(content="two")])
    async with client_for(runtime, model) as (client, app):
        body = query()
        sse(await client.post("/dataagent/stream", json=body))
        assert (await client.post("/dataagent/stream", json=body)).status_code == 409
        body["runId"] = str(uuid4())
        assert (await client.post("/dataagent/stream", json=body)).status_code == 409
        sse(await client.post("/dataagent/stream", json=query("other", thread="other")))
        messages = (await client.get("/sessions/other")).json()["messages"]
        assert len(messages) == 2 and messages[0]["content"] == "other"


async def test_concurrent_same_thread_rejected(runtime):
    ready, release = asyncio.Event(), asyncio.Event()
    class WaitingModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            ready.set()
            await release.wait()
            return await super()._agenerate(*args, **kwargs)
    async with client_for(runtime, WaitingModel(responses=[AIMessage(content="done")])) as (client, app):
        first = asyncio.create_task(client.post("/dataagent/stream", json=query()))
        await asyncio.wait_for(ready.wait(), 5)
        try:
            assert (await client.post("/dataagent/stream", json=query())).status_code == 409
        finally:
            release.set()
        assert terminal(sse(await first)) == ["RUN_FINISHED"]


async def test_deadline_covers_iteration_and_releases_session(runtime):
    runtime = replace(runtime, settings=runtime.settings.model_copy(update={"dataagent": runtime.settings.dataagent.model_copy(update={
        "limits": runtime.settings.dataagent.limits.model_copy(update={"timeout_seconds": 0.1}),
    })}))
    class NeverModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            await asyncio.Event().wait()
    async with client_for(runtime, NeverModel(responses=[])) as (client, app):
        events = sse(await client.post("/dataagent/stream", json=query()))
        assert terminal(events) == ["RUN_ERROR"]
        assert events[-1]["code"] == "TimeoutError"
        assert (await client.get("/sessions/thread-1")).status_code == 200


async def test_child_internal_events_hidden(runtime, monkeypatch):
    import importlib

    from dataagent.agent import build_agent

    recorder = RecordingCallback()
    monkeypatch.setattr(importlib.import_module("restapi.agents"), "build_agent",
                        lambda *args, **kwargs: build_agent(*args, **kwargs).with_config(callbacks=[recorder]))
    model = ScriptedModel(responses=[
        call("task", {"subagent_type": "general-purpose", "description": "statistics"}),
        call("common__summarize_numbers", {"numbers": [1, 2]}),
        AIMessage(content="child-private-answer"), AIMessage(content="root-answer"),
    ])
    async with client_for(runtime, model) as (client, app):
        response = await client.post("/dataagent/stream", json=query("delegate"))
        events = sse(response)
        assert terminal(events) == ["RUN_FINISHED"]
        starts = [event for event in events if event["type"] == "TOOL_CALL_START"]
        assert [event["toolCallName"] for event in starts] == ["task"]
        assert not any(event["type"].startswith("SUBAGENT_") for event in events)
        starts = {name: details for name, event, details in recorder.events if event == "on_chain_start"}
        root, child = starts["dataagent-v2"], starts["general-purpose"]
        assert child["parent_run_id"] is not None
        assert child["run_id"] != root["run_id"]
        assert child["thread_id"] == root["thread_id"] == "thread-1"
        assert not recorder.active


async def test_provider_failure_after_actual_streamed_tokens(runtime):
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk

    class BrokenStream(ScriptedModel):
        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            self.cursor += 1
            yield ChatGenerationChunk(message=AIMessageChunk(content="partial provider output"))
            raise RuntimeError("Provider stream failed after output")

    model = BrokenStream(responses=[])
    async with client_for(runtime, model) as (client, app):
        events = sse(await client.post("/dataagent/stream", json=query()))
        assert terminal(events) == ["RUN_ERROR"]
        assert "Provider stream failed" in events[-1]["message"]
        assert any("partial provider output" in str(event.get("delta", "")) for event in events)
        assert model.cursor == 1


async def test_real_http_disconnect_cancels_model_and_releases_session(runtime, monkeypatch):
    import importlib
    import socket

    import uvicorn
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk

    from dataagent.agent import build_agent

    cancelled = asyncio.Event()
    recorder = RecordingCallback()
    monkeypatch.setattr(importlib.import_module("restapi.agents"), "build_agent",
                        lambda *args, **kwargs: build_agent(*args, **kwargs).with_config(callbacks=[recorder]))

    class WaitingStream(ScriptedModel):
        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            try:
                yield ChatGenerationChunk(message=AIMessageChunk(content="partial output"))
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    app = create_app(runtime, model=WaitingStream(responses=[]))
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, timeout_graceful_shutdown=2))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
            ) as client:
                body = query()
                async with client.stream("POST", "/dataagent/stream", json=body) as response:
                    async for line in response.aiter_lines():
                        if "partial output" in line:
                            break
                await asyncio.wait_for(cancelled.wait(), 5)
                async with asyncio.timeout(5):
                    while (await app.state.sessions.get(runtime.paths.user_id, "thread-1"))["status"] == "running":
                        await asyncio.sleep(0.01)
                restored = await client.get("/sessions/thread-1")
                assert restored.status_code == 200
                assert restored.json()["status"] == "interrupted"
                assert not recorder.active
                assert recorder.events[-1][1] == "on_chain_error"
                traces = list(runtime.paths.for_session("thread-1").traces.glob("*.json"))
                assert len(traces) == 1
                trace = traces[0]
                data = json.loads(trace.read_text())
                root, = [span for resource in data["resourceSpans"]
                         for scope in resource["scopeSpans"] for span in scope["spans"]
                         if not span.get("parentSpanId")]
                assert root["status"]["code"] == 2  # OTLP STATUS_CODE_ERROR
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


@pytest.mark.parametrize("scenario", ["success", "tool_error", "model_error"])
async def test_sdk_and_http_share_hook_lifecycle(runtime, monkeypatch, scenario):
    import importlib

    from conftest import RecordingCallback
    from langchain_core.messages import HumanMessage

    from dataagent.agent import build_agent

    responses = [] if scenario == "model_error" else [
        call("common__summarize_numbers", {"numbers": "wrong" if scenario == "tool_error" else [1, 2, 3]}),
        AIMessage(content="result"),
    ]
    sdk_hooks, http_hooks = RecordingCallback(), RecordingCallback()
    graph = build_agent(runtime, model=ScriptedModel(responses=responses)).with_config(callbacks=[sdk_hooks])
    try:
        await graph.ainvoke({"messages": [HumanMessage(content="test")]})
    except RuntimeError:
        assert scenario == "model_error"
    monkeypatch.setattr(importlib.import_module("restapi.agents"), "build_agent",
                        lambda *args, **kwargs: build_agent(*args, **kwargs).with_config(callbacks=[http_hooks]))
    async with client_for(runtime, ScriptedModel(responses=responses)) as (client, app):
        events = sse(await client.post("/dataagent/stream", json=query()))
        assert terminal(events) == (["RUN_ERROR"] if scenario == "model_error" else ["RUN_FINISHED"])
    assert [(agent, event) for agent, event, _ in sdk_hooks.events] == [
        (agent, event) for agent, event, _ in http_hooks.events
    ]
    assert not sdk_hooks.active and not http_hooks.active


@pytest.mark.parametrize("event", ["before_agent", "before_model", "after_model", "before_tool", "after_tool", "after_agent"])
async def test_configured_hook_error_reaches_http_without_false_success_or_retry(runtime, tmp_path, event):
    import json

    root = tmp_path / "policy"
    root.mkdir()
    signature = ("request, result" if event == "after_tool" else "request"
                 if event == "before_tool" else "state, runtime")
    (root / "policy.py").write_text(
        f"async def handle({signature}, *, params):\n    raise RuntimeError('Policy failure')\n"
    )
    (root / ".plugin.json").write_text(json.dumps({
        "schema_version": 2, "id": "policy",
        "hooks": [{"entrypoint": "policy.py:handle", "event": event}],
    }))
    runtime = replace(runtime, settings=runtime.settings.model_copy(update={"plugins": runtime.settings.plugins.model_copy(update={
        "enabled": ["common", "policy"], "paths": [root],
    })}))
    model = ScriptedModel(responses=[
        call("common__summarize_numbers", {"numbers": [1]}), AIMessage(content="Original successful answer"),
    ])
    async with client_for(runtime, model) as (client, app):
        events = sse(await client.post("/dataagent/stream", json=query()))
        assert terminal(events) == ["RUN_ERROR"]
        assert events[-1]["code"] == "RuntimeError"
        assert "Policy failure" in events[-1]["message"]
        assert (await client.get("/sessions/thread-1")).json()["newThreadRequired"]
    assert len(model.requests) == (0 if event in {"before_agent", "before_model"}
                                   else 2 if event == "after_agent" else 1)
