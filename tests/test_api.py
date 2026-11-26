import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from conftest import RecordingCallback, ScriptedModel, call
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk

from dataagent.bootstrap import LaunchOptions, prepare_runtime
from dataagent.server.app import create_app


def test_continuation_configurable_is_the_saved_checkpoint_or_the_thread_head():
    from dataagent.server.sessions import continuation_configurable

    assert continuation_configurable("thread-1", None) == {"thread_id": "thread-1"}
    assert continuation_configurable("thread-1", {"last_checkpoint_id": None}) == {
        "thread_id": "thread-1"
    }
    assert continuation_configurable("thread-1", {"last_checkpoint_id": "cp-1"}) == {
        "thread_id": "thread-1",
        "checkpoint_id": "cp-1",
    }


def query(text="hello", thread="thread-1", **kwargs):
    return {
        "threadId": thread,
        "runId": str(uuid4()),
        "state": {},
        "messages": [{"id": str(uuid4()), "role": "user", "content": text}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
        **kwargs,
    }


@asynccontextmanager
async def client_for(runtime, model):
    app = create_app(runtime, instance_id="test-instance", model=model)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8787",
        ) as client:
            yield client, app


def sse(response):
    assert response.status_code == 200, response.text
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


def terminal(events):
    return [event["type"] for event in events if event["type"] in ("RUN_FINISHED", "RUN_ERROR")]


async def test_health_and_no_credential_disclosure(runtime):
    async with client_for(runtime, ScriptedModel(responses=[])) as (client, app):
        health = await client.get("/healthz")
        body = health.json()
        conversation = body["data"]["conversation"]
        assert body["success"] is True
        assert body["data"]["status"] == "ok"
        assert conversation["ready"] is True
        assert conversation["instanceId"] == "test-instance"
        assert "stateDir" not in conversation
        assert conversation["home"] == str(runtime.paths.home)
        assert conversation["user"] == runtime.paths.user_id
        assert "sources" not in conversation
        assert "test-secret" not in health.text
        assert (await client.get("/.well-known/agent-card.json")).status_code == 200
        assert (await client.get("/healthz", headers={"authorization": ""})).status_code == 200
        assert (
            await client.get("/healthz", headers={"origin": "https://example.com"})
        ).status_code == 200


async def test_health_derives_model_and_plugins_from_replaced_settings(runtime):
    original = runtime
    model = runtime.settings.models["primary"].model_copy(update={"name": "replacement-model"})
    settings = runtime.settings.model_copy(
        update={
            "models": {**runtime.settings.models, "alternate": model},
            "dataagent": runtime.settings.dataagent.model_copy(
                update={
                    "model": "alternate",
                }
            ),
            "plugins": runtime.settings.plugins.model_copy(update={"enabled": ()}),
        }
    )
    runtime = replace(runtime, settings=settings)
    assert runtime.report is original.report
    assert original.model_name == "test-model"
    assert runtime.model_name == "replacement-model"

    async with client_for(runtime, ScriptedModel(responses=[])) as (client, app):
        assert (await client.get("/healthz")).json()["data"]["conversation"] == {
            "ready": True,
            "protocol": "dataagent",
            "instanceId": "test-instance",
            "model": "replacement-model",
            "plugins": [],
            "home": str(runtime.paths.home),
            "entry": None,
            "user": runtime.paths.user_id,
            "timeoutSeconds": runtime.timeout_seconds,
        }


async def test_stream_two_rounds_and_restart(runtime):
    model = ScriptedModel(
        responses=[AIMessage(content="first answer"), AIMessage(content="second answer")]
    )
    async with client_for(runtime, model) as (client, app):
        for text in ["first", "second"]:
            events = sse(await client.post("/api/v1/runs", json=query(text)))
            assert events[0]["type"] == "RUN_STARTED"
            assert terminal(events) == ["RUN_FINISHED"]
        snapshots = [event for event in events if event["type"] == "MESSAGES_SNAPSHOT"]
        assert len(snapshots) == 1
        assert len(snapshots[0]["messages"]) == 4
        assert events[-1]["metadata"]["durationMs"] >= 0
        assert (await client.get("/api/v1/sessions")).json()["data"]["sessions"][0][
            "threadId"
        ] == "thread-1"
    async with client_for(
        runtime, ScriptedModel(responses=[AIMessage(content="after restart")])
    ) as (client, app):
        restored = (await client.get("/api/v1/sessions/thread-1")).json()["data"]
        assert len(restored["messages"]) == 4
        events = sse(await client.post("/api/v1/runs", json=query("third")))
        assert terminal(events) == ["RUN_FINISHED"]
        assert len((await client.get("/api/v1/sessions/thread-1")).json()["data"]["messages"]) == 6


async def test_empty_inputs_write_and_restore_outputs_after_restart(runtime):
    assert runtime.paths.workspaces == ()
    session = runtime.paths.for_session("thread-1")
    output = str(session.outputs / "result.txt")
    model = ScriptedModel(
        responses=[
            call("write_file", {"file_path": output, "content": "saved-output"}),
            AIMessage(content="saved"),
        ]
    )
    async with client_for(runtime, model) as (client, _):
        assert terminal(sse(await client.post("/api/v1/runs", json=query()))) == ["RUN_FINISHED"]
    assert (session.outputs / "result.txt").read_text() == "saved-output"
    from dataagent.bootstrap.paths import read_session_binding

    assert read_session_binding(session).workspaces == ()

    model = ScriptedModel(
        responses=[
            call("read_file", {"file_path": output}),
            AIMessage(content="restored"),
        ]
    )
    async with client_for(runtime, model) as (client, _):
        assert (await client.get("/api/v1/sessions/thread-1")).json()["data"][
            "threadId"
        ] == "thread-1"
        assert terminal(sse(await client.post("/api/v1/runs", json=query("read it")))) == [
            "RUN_FINISHED"
        ]
    assert any(
        "saved-output" in str(message.content)
        for message in model.requests[-1]
        if message.type == "tool" and message.name == "read_file"
    )


async def test_error_terminal_and_last_successful_checkpoint(runtime):
    model = ScriptedModel(responses=[AIMessage(content="saved answer")])
    async with client_for(runtime, model) as (client, app):
        sse(await client.post("/api/v1/runs", json=query("saved")))
        events = sse(await client.post("/api/v1/runs", json=query("will fail")))
        assert terminal(events) == ["RUN_ERROR"]
        assert "ran out" in events[-1]["message"]
        assert len(model.requests) == 2
        restored = (await client.get("/api/v1/sessions/thread-1")).json()["data"]
        assert restored["status"] == "error"
        contents = [message.get("content") for message in restored["messages"]]
        assert "saved" in contents and "will fail" in contents
        assert "Restored the saved checkpoint" in restored["notice"]
    model = ScriptedModel(responses=[AIMessage(content="recovered")])
    async with client_for(runtime, model) as (client, app):
        events = sse(await client.post("/api/v1/runs", json=query("continue")))
        assert terminal(events) == ["RUN_FINISHED"]
        contents = [message.content for message in model.requests[0]]
        assert "will fail" in contents
        assert "saved" in contents and "continue" in contents


async def test_first_run_failure_can_be_retried_on_the_same_thread(runtime):
    async with client_for(runtime, ScriptedModel(responses=[])) as (client, app):
        assert terminal(sse(await client.post("/api/v1/runs", json=query("first")))) == [
            "RUN_ERROR"
        ]
        restored = (await client.get("/api/v1/sessions/thread-1")).json()["data"]
        assert restored["status"] == "error"
        assert any(message.get("content") == "first" for message in restored["messages"])
    model = ScriptedModel(responses=[AIMessage(content="recovered")])
    async with client_for(runtime, model) as (client, app):
        events = sse(await client.post("/api/v1/runs", json=query("again")))
        assert terminal(events) == ["RUN_FINISHED"]
        seen = [message.content for message in model.requests[-1]]
        assert "first" in seen
        assert "again" in seen


async def test_failure_log_and_sse_include_only_redacted_exception_chain(runtime, caplog):
    class FailedModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            cause = OSError("socket failed: test-secret Bearer transport-secret\n\x1b[31m")
            raise ConnectionError("Connection error.") from cause

    caplog.set_level("ERROR", logger="dataagent.api")
    body = query()
    async with client_for(runtime, FailedModel(responses=[])) as (client, _):
        events = sse(await client.post("/api/v1/runs", json=body))
    assert terminal(events) == ["RUN_ERROR"]
    (record,) = [
        r for r in caplog.records if r.name == "dataagent.api" and "RUN_ERROR" in r.getMessage()
    ]
    message = record.getMessage()
    assert body["runId"] in message and body["threadId"] in message
    assert (
        "stage=agent_stream" in message and "code=INTERNAL_ERROR error=ConnectionError" in message
    )
    assert "OSError: socket failed" in message and "OSError: socket failed" in events[-1]["message"]
    assert "test-secret" not in message and "transport-secret" not in message
    assert (
        "test-secret" not in events[-1]["message"]
        and "transport-secret" not in events[-1]["message"]
    )
    assert "\x1b" not in message and "\n" not in message
    assert record.exc_info is None  # Raw exception objects stay out of the public log record.
    assert "_agenerate" in record.stack and "OSError" in record.stack
    assert "test-secret" not in record.stack and "transport-secret" not in record.stack


async def test_upstream_run_error_is_also_logged_redacted(runtime, caplog, monkeypatch):
    from ag_ui.core import RunErrorEvent

    async def fail(self, body):
        yield RunErrorEvent(
            message="upstream failed: test-secret",
            code="provider test-secret",
            raw_event={"apiKey": "test-secret", "detail": "do-not-forward"},
        )

    monkeypatch.setattr("ag_ui_langgraph.LangGraphAgent.run", fail)
    caplog.set_level("ERROR", logger="dataagent.api")
    async with client_for(runtime, ScriptedModel(responses=[])) as (client, _):
        events = sse(await client.post("/api/v1/runs", json=query()))
    assert terminal(events) == ["RUN_ERROR"]
    assert events[-1]["message"] == "upstream failed: [REDACTED]"
    assert events[-1]["code"] == "INTERNAL_ERROR"
    assert "rawEvent" not in events[-1]
    assert "raw_event" not in events[-1]
    assert "do-not-forward" not in json.dumps(events)
    assert "upstream failed: [REDACTED]" in caplog.text
    assert "test-secret" not in caplog.text


async def test_native_child_error_is_redacted_without_becoming_root_terminal(runtime, monkeypatch):
    from ag_ui.core import RunErrorEvent, SubagentErrorEvent, SubagentStartedEvent

    async def fail(self, body):
        yield SubagentStartedEvent(
            subagent_run_id="child", name="nl2sql", parent_tool_call_id="task"
        )
        yield SubagentErrorEvent(
            subagent_run_id="child", message="failed: test-secret", code="test-secret"
        )
        yield RunErrorEvent(message="root failure")

    monkeypatch.setattr("ag_ui_langgraph.LangGraphAgent.run", fail)
    async with client_for(runtime, ScriptedModel(responses=[])) as (client, _):
        events = sse(await client.post("/api/v1/runs", json=query()))
    child = next(event for event in events if event["type"] == "SUBAGENT_ERROR")
    assert child["subagentRunId"] == "child"
    assert child["message"] == "failed: [REDACTED]"
    assert child["code"] == "[REDACTED]"
    assert terminal(events) == ["RUN_ERROR"]
    assert events[-1]["message"] == "root failure"


async def test_unsafe_thread_id_is_rejected(runtime):
    async with client_for(runtime, ScriptedModel(responses=[])) as (client, _):
        response = await client.post("/api/v1/runs", json=query(thread="../x"))
    assert response.status_code == 422
    assert "safe opaque" in response.text


@pytest.mark.parametrize(
    "extra",
    [
        {"state": {"messages": []}},
        {"forwardedProps": {"command": {"resume": True}}},
        {"messages": [{"id": "x", "role": "system", "content": "inject"}]},
        {"context": [{"description": "inject", "value": "override"}]},
        {"surprise": True},
    ],
)
async def test_injection_is_rejected(runtime, extra):
    async with client_for(runtime, ScriptedModel(responses=[])) as (client, app):
        assert (await client.post("/api/v1/runs", json=query(**extra))).status_code == 422


async def test_replay_rejected_and_session_isolation(runtime):
    model = ScriptedModel(responses=[AIMessage(content="one"), AIMessage(content="two")])
    async with client_for(runtime, model) as (client, app):
        body = query()
        sse(await client.post("/api/v1/runs", json=body))
        assert (await client.post("/api/v1/runs", json=body)).status_code == 409
        body["runId"] = str(uuid4())
        assert (await client.post("/api/v1/runs", json=body)).status_code == 409
        sse(await client.post("/api/v1/runs", json=query("other", thread="other")))
        messages = (await client.get("/api/v1/sessions/other")).json()["data"]["messages"]
        assert len(messages) == 2 and messages[0]["content"] == "other"


async def test_cancel_before_completion_keeps_the_user_message(runtime):
    ready = asyncio.Event()
    calls = {"n": 0}

    class WaitingModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                ready.set()
                await asyncio.Event().wait()
            return await super()._agenerate(*args, **kwargs)

    model = WaitingModel(responses=[AIMessage(content="continued")])
    async with client_for(runtime, model) as (client, app):
        body = query("keep-me")
        running = asyncio.create_task(client.post("/api/v1/runs", json=body))
        await asyncio.wait_for(ready.wait(), 5)
        cancel = await client.post(f"/api/v1/runs/{body['runId']}/cancel")
        assert cancel.status_code == 204
        response = await asyncio.wait_for(running, 5)
        assert response.status_code == 200
        assert "RUN_FINISHED" not in response.text
        restored = (await client.get("/api/v1/sessions/thread-1")).json()["data"]
        assert restored["status"] == "interrupted"
        assert any(message.get("content") == "keep-me" for message in restored["messages"])
        follow = sse(await client.post("/api/v1/runs", json=query("next")))
        assert terminal(follow) == ["RUN_FINISHED"]
        assert any(
            message.content == "keep-me"
            for message in model.requests[-1]
            if getattr(message, "type", None) == "human"
        )


async def test_session_reads_match_while_running_and_after_cancel(runtime):
    ready = asyncio.Event()
    calls = {"n": 0}

    class WaitingModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                ready.set()
                await asyncio.Event().wait()
            return await super()._agenerate(*args, **kwargs)

    model = WaitingModel(responses=[AIMessage(content="continued")])
    async with client_for(runtime, model) as (client, _):
        body = query("keep-me")
        running = asyncio.create_task(client.post("/api/v1/runs", json=body))
        await asyncio.wait_for(ready.wait(), 5)
        tui_view = (await client.get("/api/v1/sessions/thread-1")).json()["data"]
        assert tui_view["status"] == "running"
        assert "newThreadRequired" not in tui_view
        assert tui_view["notice"] is None
        assert (await client.post(f"/api/v1/runs/{body['runId']}/cancel")).status_code == 204
        await asyncio.wait_for(running, 5)
        tui_after = (await client.get("/api/v1/sessions/thread-1")).json()["data"]
        assert tui_after["status"] == "interrupted"
        assert "Restored the saved checkpoint" in tui_after["notice"]
        assert any(message.get("content") == "keep-me" for message in tui_after["messages"])
        follow = sse(await client.post("/api/v1/runs", json=query("next")))
        assert terminal(follow) == ["RUN_FINISHED"]
        assert any(
            message.content == "keep-me"
            for message in model.requests[-1]
            if getattr(message, "type", None) == "human"
        )


async def test_cancel_after_success_keeps_the_cancelled_message_for_the_next_turn(runtime):
    ready = asyncio.Event()
    calls = {"n": 0}

    class WaitingModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                ready.set()
                await asyncio.Event().wait()
            return await super()._agenerate(*args, **kwargs)

    model = WaitingModel(
        responses=[
            AIMessage(content="saved answer"),
            AIMessage(content="continued"),
        ]
    )
    async with client_for(runtime, model) as (client, app):
        sse(await client.post("/api/v1/runs", json=query("saved")))
        body = query("BLUE-42")
        running = asyncio.create_task(client.post("/api/v1/runs", json=body))
        await asyncio.wait_for(ready.wait(), 5)
        mid = (await client.get("/api/v1/sessions/thread-1")).json()["data"]
        assert mid["status"] == "running"
        assert "saved" in [message.get("content") for message in mid["messages"]]
        assert (await client.post(f"/api/v1/runs/{body['runId']}/cancel")).status_code == 204
        response = await asyncio.wait_for(running, 5)
        assert "RUN_FINISHED" not in response.text
        restored = (await client.get("/api/v1/sessions/thread-1")).json()["data"]
        assert restored["status"] == "interrupted"
        contents = [message.get("content") for message in restored["messages"]]
        assert "saved" in contents and "BLUE-42" in contents
        follow = sse(await client.post("/api/v1/runs", json=query("what was the code")))
        assert terminal(follow) == ["RUN_FINISHED"]
        seen = [message.content for message in model.requests[-1]]
        assert "BLUE-42" in seen and "saved" in seen


async def test_concurrent_same_thread_rejected(runtime):
    ready, release = asyncio.Event(), asyncio.Event()

    class WaitingModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            ready.set()
            await release.wait()
            return await super()._agenerate(*args, **kwargs)

    async with client_for(runtime, WaitingModel(responses=[AIMessage(content="done")])) as (
        client,
        app,
    ):
        first = asyncio.create_task(client.post("/api/v1/runs", json=query()))
        await asyncio.wait_for(ready.wait(), 5)
        try:
            assert (await client.post("/api/v1/runs", json=query())).status_code == 409
        finally:
            release.set()
        assert terminal(sse(await first)) == ["RUN_FINISHED"]


async def test_deadline_covers_iteration_and_releases_session(runtime):
    runtime = replace(
        runtime,
        settings=runtime.settings.model_copy(
            update={
                "dataagent": runtime.settings.dataagent.model_copy(
                    update={
                        "limits": runtime.settings.dataagent.limits.model_copy(
                            update={"timeout_seconds": 0.1}
                        ),
                    }
                )
            }
        ),
    )

    class NeverModel(ScriptedModel):
        async def _agenerate(self, *args, **kwargs):
            await asyncio.Event().wait()

    async with client_for(runtime, NeverModel(responses=[])) as (client, app):
        events = sse(await client.post("/api/v1/runs", json=query()))
        assert terminal(events) == ["RUN_ERROR"]
        assert events[-1]["code"] == "TIMEOUT"
        assert (await client.get("/api/v1/sessions/thread-1")).status_code == 200


async def test_native_child_progress_visible_but_internal_content_hidden(runtime, monkeypatch):
    import importlib

    from dataagent.assembly.graph import build_graph

    recorder = RecordingCallback()
    monkeypatch.setattr(
        importlib.import_module("dataagent.client"),
        "build_graph",
        lambda *args, **kwargs: build_graph(*args, **kwargs).with_config(callbacks=[recorder]),
    )
    model = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "general-purpose", "description": "statistics"}),
            call("common__summarize_numbers", {"numbers": [1, 2]}),
            AIMessage(content="child-private-answer"),
            AIMessage(content="root-answer"),
        ]
    )
    async with client_for(runtime, model) as (client, app):
        response = await client.post("/api/v1/runs", json=query("delegate"))
        events = sse(response)
        assert terminal(events) == ["RUN_FINISHED"]
        starts = [event for event in events if event["type"] == "TOOL_CALL_START"]
        assert [event["toolCallName"] for event in starts] == ["task"]
        children = [event for event in events if event["type"] == "SUBAGENT_STARTED"]
        assert len(children) == 1 and children[0]["parentToolCallId"] == starts[0]["toolCallId"]
        sid = children[0]["subagentRunId"]
        assert any(
            event["type"] == "STEP_STARTED" and event.get("subagentRunId") == sid
            for event in events
        )
        assert any(
            event["type"] == "SUBAGENT_FINISHED" and event["subagentRunId"] == sid
            for event in events
        )
        assert not any("child-private-answer" in event.get("delta", "") for event in events)
        starts = {
            name: details for name, event, details in recorder.events if event == "on_chain_start"
        }
        root, child = starts["dataagent"], starts["general-purpose"]
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
        events = sse(await client.post("/api/v1/runs", json=query()))
        assert terminal(events) == ["RUN_ERROR"]
        assert "Provider stream failed" in events[-1]["message"]
        assert any("partial provider output" in str(event.get("delta", "")) for event in events)
        assert model.cursor == 1


@pytest.mark.parametrize("failed_message", ["http.response.start", "http.response.body"])
async def test_sse_send_failure_releases_session_and_restores_log_context(runtime, failed_message):
    from dataagent.diagnostics.logging import log_context

    async with client_for(
        runtime, ScriptedModel(responses=[AIMessage(content="retry succeeded")])
    ) as (client, app):
        body = query()
        received = False
        failed = False
        original = log_context.get().copy()

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {
                    "type": "http.request",
                    "body": json.dumps(body).encode(),
                    "more_body": False,
                }
            await asyncio.Event().wait()

        async def send(message):
            nonlocal failed
            if message["type"] == failed_message:
                failed = True
                assert log_context.get() == {
                    "requestId": "send-failure",
                    "threadId": body["threadId"],
                    "runId": body["runId"],
                    "stage": "run",
                }
                raise OSError("client disconnected")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/runs",
            "raw_path": b"/api/v1/runs",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json"), (b"x-request-id", b"send-failure")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8790),
        }
        async with asyncio.timeout(5):
            await app(scope, receive, send)
        assert failed
        assert log_context.get() == original
        assert not app.state.active_runs
        row = await app.state.sessions.get(runtime.paths.user_id, body["threadId"])
        assert row["status"] == "interrupted"
        response = await client.post("/api/v1/runs", json=query("retry"))
        assert terminal(sse(response)) == ["RUN_FINISHED"]


async def test_real_http_disconnect_cancels_model_and_releases_session(runtime, monkeypatch):
    import importlib
    import socket

    import uvicorn
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk

    from dataagent.assembly.graph import build_graph

    cancelled = asyncio.Event()
    recorder = RecordingCallback()
    monkeypatch.setattr(
        importlib.import_module("dataagent.client"),
        "build_graph",
        lambda *args, **kwargs: build_graph(*args, **kwargs).with_config(callbacks=[recorder]),
    )

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
        server = uvicorn.Server(
            uvicorn.Config(app, log_config=None, access_log=False, timeout_graceful_shutdown=2)
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
            ) as client:
                body = query()
                async with client.stream("POST", "/api/v1/runs", json=body) as response:
                    async for line in response.aiter_lines():
                        if "partial output" in line:
                            break
                await asyncio.wait_for(cancelled.wait(), 5)
                async with asyncio.timeout(5):
                    while (await app.state.sessions.get(runtime.paths.user_id, "thread-1"))[
                        "status"
                    ] == "running":
                        await asyncio.sleep(0.01)
                restored = await client.get("/api/v1/sessions/thread-1")
                assert restored.status_code == 200
                assert restored.json()["data"]["status"] == "interrupted"
                assert not recorder.active
                assert recorder.events[-1][1] == "on_chain_error"
                traces = list(runtime.paths.for_session("thread-1").traces.glob("*.json"))
                assert len(traces) == 1
                trace = traces[0]
                data = json.loads(trace.read_text())
                (root,) = [
                    span
                    for resource in data["resourceSpans"]
                    for scope in resource["scopeSpans"]
                    for span in scope["spans"]
                    if not span.get("parentSpanId")
                ]
                assert root["status"]["code"] == 2  # OTLP STATUS_CODE_ERROR
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)


@pytest.mark.parametrize("scenario", ["success", "tool_error", "model_error"])
async def test_sdk_and_http_share_hook_lifecycle(runtime, monkeypatch, scenario):
    import importlib

    from conftest import RecordingCallback
    from langchain_core.messages import HumanMessage

    from dataagent.assembly.graph import build_graph

    responses = (
        []
        if scenario == "model_error"
        else [
            call(
                "common__summarize_numbers",
                {"numbers": "wrong" if scenario == "tool_error" else [1, 2, 3]},
            ),
            AIMessage(content="result"),
        ]
    )
    sdk_hooks, http_hooks = RecordingCallback(), RecordingCallback()
    graph = build_graph(runtime, model=ScriptedModel(responses=responses)).with_config(
        callbacks=[sdk_hooks]
    )
    try:
        await graph.ainvoke({"messages": [HumanMessage(content="test")]})
    except RuntimeError:
        assert scenario == "model_error"
    monkeypatch.setattr(
        importlib.import_module("dataagent.client"),
        "build_graph",
        lambda *args, **kwargs: build_graph(*args, **kwargs).with_config(callbacks=[http_hooks]),
    )
    async with client_for(runtime, ScriptedModel(responses=responses)) as (client, app):
        events = sse(await client.post("/api/v1/runs", json=query()))
        assert terminal(events) == (
            ["RUN_ERROR"] if scenario == "model_error" else ["RUN_FINISHED"]
        )
    assert [(agent, event) for agent, event, _ in sdk_hooks.events] == [
        (agent, event) for agent, event, _ in http_hooks.events
    ]
    assert not sdk_hooks.active and not http_hooks.active


@pytest.mark.parametrize(
    "event",
    ["before_agent", "before_model", "after_model", "before_tool", "after_tool", "after_agent"],
)
async def test_configured_hook_error_reaches_http_without_false_success_or_retry(
    runtime, tmp_path, event
):
    import json

    root = tmp_path / "policy"
    root.mkdir()
    signature = (
        "request, result"
        if event == "after_tool"
        else "request"
        if event == "before_tool"
        else "state, runtime"
    )
    (root / "policy.py").write_text(
        f"async def handle({signature}, *, params):\n    raise RuntimeError('Policy failure')\n"
    )
    (root / ".plugin.yaml").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "id": "policy",
                "hooks": [{"entrypoint": "policy.py:handle", "event": event}],
            }
        )
    )
    runtime = replace(
        runtime,
        settings=runtime.settings.model_copy(
            update={
                "plugins": runtime.settings.plugins.model_copy(
                    update={
                        "enabled": ["common", "policy"],
                        "paths": [root],
                    }
                )
            }
        ),
    )
    model = ScriptedModel(
        responses=[
            call("common__summarize_numbers", {"numbers": [1]}),
            AIMessage(content="Original successful answer"),
        ]
    )
    async with client_for(runtime, model) as (client, app):
        events = sse(await client.post("/api/v1/runs", json=query()))
        assert terminal(events) == ["RUN_ERROR"]
        assert events[-1]["code"] == "INTERNAL_ERROR"
        assert "Policy failure" in events[-1]["message"]
        assert (await client.get("/api/v1/sessions/thread-1")).json()["data"]["status"] == "error"
    assert len(model.requests) == (
        0 if event in {"before_agent", "before_model"} else 2 if event == "after_agent" else 1
    )


class StreamingScriptedModel(ScriptedModel):
    """Scripted replies that also implement streaming, so AG-UI would see tokens."""

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        result = ScriptedModel._generate(self, messages, stop, run_manager, **kwargs)
        message = result.generations[0].message
        text = message.content if isinstance(message.content, str) else ""
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                additional_kwargs={"reasoning_content": "hidden-thought"},
                id=message.id,
            )
        )
        yield ChatGenerationChunk(message=AIMessageChunk(content=text, id=message.id))


def _write_entry_stream_plugin(root, *, model_source="services"):
    root.mkdir(parents=True)
    (root / "subagents").mkdir()
    (root / ".plugin.yaml").write_text(
        json.dumps(
            {
                "id": "direct",
                "subagents": ["subagents/direct.yaml"],
            }
        )
    )
    (root / "subagents" / "direct.yaml").write_text(
        json.dumps(
            {
                "kind": "compiled",
                "name": "direct",
                "description": "Entry model calls.",
                "entrypoint": "subagents/direct.py:build",
            }
        )
    )
    lookup = "runtime.context.get_model()" if model_source == "runtime" else "services.get_model()"
    signature = "state, runtime" if model_source == "runtime" else "state"
    (root / "subagents" / "direct.py").write_text(
        "from langchain_core.messages import AIMessage, HumanMessage\n"
        "from langgraph.graph import END, START, MessagesState, StateGraph\n"
        "\n"
        "def build(params, services):\n"
        f"    async def work({signature}):\n"
        f"        model = {lookup}\n"
        "        await model.ainvoke([HumanMessage(content='internal')])\n"
        "        return {'messages': [AIMessage(content='visible-reply')]}\n"
        "\n"
        "    graph = StateGraph(MessagesState)\n"
        "    graph.add_node('work', work)\n"
        "    graph.add_edge(START, 'work')\n"
        "    graph.add_edge('work', END)\n"
        "    return graph.compile()\n",
        encoding="utf-8",
    )


def _entry_runtime(runtime, tmp_path, **plugin):
    _write_entry_stream_plugin(tmp_path / "home" / "plugins" / "direct", **plugin)
    (runtime.paths.home / "config.yaml").write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": "test-model",
                        "base_url": "https://example.invalid/v1",
                        "api_key": "test-secret",
                    }
                },
                "plugins": {"enabled": ["direct"]},
                "dataagent": {"entry": "direct"},
            }
        )
    )
    return prepare_runtime(LaunchOptions(cwd=tmp_path))


def _assert_model_stream_visible(events):
    assert terminal(events) == ["RUN_FINISHED"]
    text = [event.get("delta", "") for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"]
    assert any("secret-token" in delta for delta in text)
    reasoning = [
        event.get("delta", "")
        for event in events
        if "REASONING" in event["type"] or "THINKING" in event["type"]
    ]
    assert any("hidden-thought" in delta for delta in reasoning)
    snapshots = [event for event in events if event["type"] == "MESSAGES_SNAPSHOT"]
    assert snapshots and "visible-reply" in json.dumps(snapshots[-1])


async def test_unknown_entry_fails_the_request_not_health(runtime):
    runtime = replace(
        runtime,
        settings=runtime.settings.model_copy(
            update={
                "dataagent": runtime.settings.dataagent.model_copy(update={"entry": "missing"}),
            }
        ),
    )
    async with client_for(runtime, ScriptedModel(responses=[])) as (client, _):
        health = await client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["data"]["conversation"]["entry"] == "missing"
        response = await client.post("/api/v1/runs", json=query())
    assert response.status_code == 409 and "missing" in response.text


@pytest.mark.parametrize("model_source", ["services", "runtime"])
async def test_compiled_entry_streams_model_chunks(runtime, tmp_path, model_source):
    runtime = _entry_runtime(runtime, tmp_path, model_source=model_source)
    model = StreamingScriptedModel(responses=[AIMessage(content="secret-token")])
    assert type(model)._astream is not ScriptedModel._astream
    async with client_for(runtime, model) as (client, _):
        stream = sse(
            await client.post(
                "/api/v1/runs",
                json=query("ask", thread=f"{model_source}-entry"),
            )
        )
    _assert_model_stream_visible(stream)


async def test_concurrent_requests_with_one_run_id_get_one_409(runtime):
    model = ScriptedModel(responses=[AIMessage(content="ok")] * 2)
    first = query(thread="thread-dup-a")
    second = query(thread="thread-dup-b", runId=first["runId"])
    async with client_for(runtime, model) as (client, _app):
        responses = await asyncio.gather(
            client.post("/api/v1/runs", json=first),
            client.post("/api/v1/runs", json=second),
        )
        # A finished run frees its runId.
        third = query(thread="thread-dup-c", runId=first["runId"])
        reused = await client.post("/api/v1/runs", json=third)
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert reused.status_code == 200
