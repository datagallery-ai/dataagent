"""Public A2A diagnostics must correlate tasks, graph runs and operator records."""

import asyncio
import json
import logging
from uuid import UUID

import httpx
import pytest
from conftest import ScriptedModel
from langchain_core.messages import AIMessage
from test_a2a_server import _GatedModel, client_for, message, rpc, stream_turn, task_of

from dataagent.diagnostics.errors import DataAgentError
from dataagent.server.a2a.agent_executor import AgentKitExecutor
from dataagent.server.app import create_app


def audit_records(caplog, event):
    return [
        r.dataagent_event
        for r in caplog.records
        if getattr(r, "dataagent_event", {}).get("event") == event
    ]


def metadata(task):
    return task["metadata"]["dataagent"]


async def test_a2a_ids_connect_response_session_trace_and_logs(runtime, caplog):
    caplog.set_level(logging.INFO)
    payload = message("hello", "correlated-session")
    async with client_for(runtime, ScriptedModel(responses=[AIMessage(content="ok")])) as client:
        response = await rpc(client, payload)
        task = task_of(response)
        details = metadata(task)
        assert str(UUID(response.headers["X-Request-ID"])) == details["requestId"]
        assert details["schemaVersion"] == 1
        assert details["messageId"] == payload["params"]["message"]["messageId"]
        assert "error" not in details
        queried = (
            await rpc(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": "query",
                    "method": "GetTask",
                    "params": {"id": task["id"]},
                },
            )
        ).json()["result"]
        assert metadata(queried) == details
    from dataagent.server.sessions import Sessions

    async with Sessions(runtime.paths.sessions_db) as sessions:
        row = await sessions.get(runtime.paths.user_id, task["contextId"])
    assert row["last_run_id"] == details["rootRunId"]
    assert row["last_checkpoint_id"]
    (trace_file,) = runtime.paths.for_session(task["contextId"]).traces.glob("*.json")
    data = json.loads(trace_file.read_text())
    spans = [
        s
        for resource in data["resourceSpans"]
        for scope in resource["scopeSpans"]
        for s in scope["spans"]
    ]
    (root,) = [s for s in spans if not s.get("parentSpanId")]
    attrs = {v["key"]: next(iter(v["value"].values())) for v in root["attributes"]}
    assert attrs["langsmith.metadata.dataagent_root_run_id"] == details["rootRunId"]
    assert attrs["langsmith.metadata.dataagent_a2a_task_id"] == task["id"]
    assert attrs["langsmith.metadata.dataagent_request_id"] == details["requestId"]
    (completed,) = audit_records(caplog, "a2a.assembly.completed")
    assert completed["task_id"] == task["id"]
    assert completed["root_run_id"] == details["rootRunId"]
    (saved,) = audit_records(caplog, "a2a.trace.saved")
    assert saved["request_id"] == details["requestId"]
    assert saved["trace_file"] == str(trace_file)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (TimeoutError(), "TIMEOUT"),
        (DataAgentError(source="llm", fact="provider unavailable"), "MODEL_ERROR"),
        (DataAgentError(source="tool", fact="tool unavailable"), "TOOL_ERROR"),
        (RuntimeError("unexpected test-secret"), "INTERNAL_ERROR"),
    ],
)
async def test_a2a_failure_codes_are_queryable_and_logged(runtime, caplog, error, code):
    caplog.set_level(logging.INFO)

    async def fail(_thread_id, _text):
        raise error

    executor = AgentKitExecutor(fail, secrets=tuple(runtime.redaction_secrets))
    async with client_for(runtime, ScriptedModel(responses=[]), executor=executor) as client:
        response = await rpc(client, message("fail", "failed-session"))
        task = task_of(response)
        assert task["status"]["state"] == "TASK_STATE_FAILED"
        details = metadata(task)
        assert details["error"]["code"] == code
        assert details["error"]["stage"] == "agent_invoke"
        assert details["requestId"] == response.headers["X-Request-ID"]
        assert "test-secret" not in response.text
        queried = (
            await rpc(
                client,
                {
                    "jsonrpc": "2.0",
                    "id": "query",
                    "method": "GetTask",
                    "params": {"id": task["id"]},
                },
            )
        ).json()["result"]
        assert metadata(queried) == details
    (failed,) = audit_records(caplog, "a2a.assembly.failed")
    assert failed["error_code"] == code
    assert failed["task_id"] == task["id"]
    assert failed["request_id"] == details["requestId"]
    assert "test-secret" not in caplog.text


async def test_a2a_rejections_have_request_ids_without_graph_runs(runtime, caplog):
    caplog.set_level(logging.INFO)
    model = ScriptedModel(responses=[])
    async with client_for(runtime, model, auth_token="private-token") as client:
        denied = await rpc(client, message("hello", "denied"))
        assert denied.status_code == 401
        UUID(denied.headers["X-Request-ID"])
        malformed = await client.post(
            "/a2a/jsonrpc",
            content="{",
            headers={
                "A2A-Version": "1.0",
                "Authorization": "Bearer private-token",
            },
        )
        assert malformed.json()["error"]["code"] == -32700
        UUID(malformed.headers["X-Request-ID"])
        blank = task_of(
            await rpc(
                client,
                message(" ", "blank"),
                headers={
                    "Authorization": "Bearer private-token",
                },
            )
        )
        assert metadata(blank)["error"]["code"] == "BAD_REQUEST"
        assert "rootRunId" not in metadata(blank)
    assert not model.requests
    assert not audit_records(caplog, "a2a.assembly.started")
    finished = audit_records(caplog, "a2a.request.finished")
    assert {r["http_status"] for r in finished} == {200, 401}
    assert any(r.get("rpc_error_code") == -32700 for r in finished)


async def test_a2a_not_ready_keeps_503_and_adds_request_id(caplog):
    caplog.set_level(logging.INFO)
    app = create_app(None, instance_id="not-ready")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await rpc(client, message("hi", "unavailable"))
    assert response.status_code == 503
    UUID(response.headers["X-Request-ID"])
    (record,) = audit_records(caplog, "a2a.request.finished")
    assert record["http_status"] == 503
    assert record["instance_id"] == "not-ready"


async def test_a2a_response_interruption_is_not_logged_as_a_completed_request(caplog):
    from dataagent.server.a2a.request_context import A2AAuditMiddleware

    caplog.set_level(logging.INFO)
    sent = []

    async def broken(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("response interrupted")

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(event):
        sent.append(event)

    middleware = A2AAuditMiddleware(broken, instance_id="broken-response")
    with pytest.raises(RuntimeError, match="response interrupted"):
        await middleware({"type": "http", "path": "/a2a/jsonrpc"}, receive, send)
    assert sent[0]["status"] == 200
    assert not audit_records(caplog, "a2a.request.finished")
    (record,) = audit_records(caplog, "a2a.request.aborted")
    assert record["http_status"] == 200


async def test_a2a_anonymous_nonblocking_query_list_cancel(runtime, caplog):
    caplog.set_level(logging.INFO)
    entered, release = asyncio.Event(), asyncio.Event()
    model = _GatedModel(
        responses=[AIMessage(content="must not complete")], entered=entered, release=release
    )
    async with client_for(runtime, model) as client:
        payload = message("wait", "cancel-session")
        payload["params"]["configuration"] = {"returnImmediately": True}
        response = await rpc(client, payload)
        task = task_of(response)
        await asyncio.wait_for(entered.wait(), 5)
        listed = await rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": "list",
                "method": "ListTasks",
                "params": {},
            },
        )
        assert task["id"] in [t["id"] for t in listed.json()["result"]["tasks"]]
        canceled_response = await rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": "cancel",
                "method": "CancelTask",
                "params": {"id": task["id"]},
            },
        )
        canceled = canceled_response.json()["result"]
        assert canceled["status"]["state"] == "TASK_STATE_CANCELED"
        assert metadata(canceled)["error"]["code"] == "CANCELLED"
        assert metadata(canceled)["requestId"] == response.headers["X-Request-ID"]
        assert canceled_response.headers["X-Request-ID"] != response.headers["X-Request-ID"]
        # CancelTask acknowledges before trace/checkpoint cleanup has finished.
        async with asyncio.timeout(5):
            while not any(
                r.get("task_id") == task["id"]
                for r in audit_records(caplog, "a2a.assembly.cancelled")
            ):
                await asyncio.sleep(0.01)
    assert not audit_records(caplog, "a2a.assembly.completed")
    assert audit_records(caplog, "a2a.assembly.cancelled")


async def test_a2a_web_conflict_has_stable_task_code(runtime):
    entered, release = asyncio.Event(), asyncio.Event()
    model = _GatedModel(
        responses=[AIMessage(content="web answer")], entered=entered, release=release
    )
    app = create_app(runtime, instance_id="conflict-test", model=model)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            running = asyncio.create_task(
                client.post("/api/v1/runs", json=stream_turn("from web", "busy-session"))
            )
            try:
                await asyncio.wait_for(entered.wait(), 5)
                failed = task_of(await rpc(client, message("from a2a", "busy-session")))
                assert metadata(failed)["error"]["code"] == "CONFLICT"
                assert metadata(failed)["error"]["stage"] == "session_prepare"
            finally:
                release.set()
                await running


async def test_a2a_parallel_requests_with_reused_rpc_id_do_not_mix(runtime, caplog):
    caplog.set_level(logging.INFO)
    entered, release = asyncio.Event(), asyncio.Event()
    model = _GatedModel(
        responses=[AIMessage(content="one"), AIMessage(content="two")],
        entered=entered,
        release=release,
    )
    async with client_for(runtime, model) as client:
        first = asyncio.create_task(rpc(client, message("first", "parallel-one")))
        second = asyncio.create_task(rpc(client, message("second", "parallel-two")))
        try:
            await asyncio.wait_for(entered.wait(), 5)
        finally:
            release.set()
        responses = await asyncio.gather(first, second)
    tasks = [task_of(r) for r in responses]
    assert len({metadata(t)["requestId"] for t in tasks}) == 2
    assert len({metadata(t)["rootRunId"] for t in tasks}) == 2
    for task in tasks:
        events = [
            r for r in audit_records(caplog, "a2a.assembly.completed") if r["task_id"] == task["id"]
        ]
        (event,) = events
        assert event["context_id"] == task["contextId"]
        assert event["request_id"] == metadata(task)["requestId"]


async def test_a2a_same_context_queue_reads_the_first_completed_checkpoint(runtime, caplog):
    caplog.set_level(logging.INFO)
    entered, release = asyncio.Event(), asyncio.Event()
    model = _GatedModel(
        responses=[AIMessage(content="first answer"), AIMessage(content="second answer")],
        entered=entered,
        release=release,
    )
    async with client_for(runtime, model) as client:
        first = asyncio.create_task(rpc(client, message("remember BLUE", "queued-session")))
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(rpc(client, message("continue", "queued-session")))
        try:
            async with asyncio.timeout(5):
                while len(audit_records(caplog, "a2a.assembly.queued")) < 2:
                    await asyncio.sleep(0.01)
            assert len(audit_records(caplog, "a2a.assembly.started")) == 1
        finally:
            release.set()
        tasks = [task_of(r) for r in await asyncio.gather(first, second)]
    contents = [m.content for m in model.requests[1]]
    assert "remember BLUE" in contents and "first answer" in contents and "continue" in contents
    assert len({metadata(t)["rootRunId"] for t in tasks}) == 2
    checkpoints = audit_records(caplog, "a2a.checkpoint.saved")
    assert checkpoints[1]["checkpoint_id_before"] == checkpoints[0]["checkpoint_id"]


async def test_a2a_cleanup_failure_preserves_primary_error_and_releases_session(
    runtime, monkeypatch, caplog
):
    from dataagent.server.sessions import SessionTurn

    async def fail_settle(self, graph, *, complete=False):
        raise OSError("checkpoint failed test-secret")

    monkeypatch.setattr(SessionTurn, "settle", fail_settle)
    caplog.set_level(logging.INFO)
    async with client_for(runtime, ScriptedModel(responses=[])) as client:
        failed = task_of(await rpc(client, message("fail", "cleanup-session")))
    assert metadata(failed)["error"]["stage"] == "agent_invoke"
    assert "ran out of responses" in metadata(failed)["error"]["message"]
    assert audit_records(caplog, "a2a.checkpoint.failed")
    assert "test-secret" not in caplog.text


async def test_a2a_checkpoint_failure_is_distinct_from_graph_failure(runtime, monkeypatch, caplog):
    from dataagent.server.sessions import SessionTurn

    async def fail_settle(self, graph, *, complete=False):
        raise OSError("checkpoint failed test-secret")

    monkeypatch.setattr(SessionTurn, "settle", fail_settle)
    caplog.set_level(logging.INFO)
    async with client_for(
        runtime, ScriptedModel(responses=[AIMessage(content="answer")])
    ) as client:
        failed = task_of(await rpc(client, message("hello", "checkpoint-session")))
    assert metadata(failed)["error"]["stage"] == "checkpoint_settle"
    assert metadata(failed)["error"]["code"] == "INTERNAL_ERROR"
    assert audit_records(caplog, "a2a.trace.saved")
    assert not audit_records(caplog, "a2a.assembly.completed")


async def test_a2a_real_execution_timeout_saves_trace_and_code(runtime):
    from dataclasses import replace

    runtime = replace(
        runtime,
        settings=runtime.settings.model_copy(
            update={
                "dataagent": runtime.settings.dataagent.model_copy(
                    update={
                        "limits": runtime.settings.dataagent.limits.model_copy(
                            update={"timeout_seconds": 0.05}
                        ),
                    }
                ),
            }
        ),
    )
    model = _GatedModel(responses=[], entered=asyncio.Event(), release=asyncio.Event())
    async with client_for(runtime, model) as client:
        failed = task_of(await rpc(client, message("wait", "timeout-session")))
        assert metadata(failed)["error"]["code"] == "TIMEOUT"
        assert metadata(failed)["error"]["stage"] == "agent_invoke"
    assert list(runtime.paths.for_session("timeout-session").traces.glob("*.json"))


async def test_a2a_explicit_token_is_redacted_from_graph_trace_and_audit(runtime, caplog):
    caplog.set_level(logging.INFO)
    async with client_for(
        runtime, ScriptedModel(responses=[]), auth_token="explicit-private-token"
    ) as client:
        failed = task_of(
            await rpc(
                client,
                message("explicit-private-token", "redacted-session"),
                headers={"Authorization": "Bearer explicit-private-token"},
            )
        )
    assert failed["status"]["state"] == "TASK_STATE_FAILED"
    (trace,) = runtime.paths.for_session("redacted-session").traces.glob("*.json")
    assert "explicit-private-token" not in trace.read_text()
    assert "explicit-private-token" not in caplog.text


def test_a2a_long_error_keeps_json_correlation_and_redacts_each_field(runtime, caplog, monkeypatch):
    from dataagent.cli import configure_logging
    from dataagent.diagnostics.render import log_event

    configured = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: configured.update(kwargs))
    configure_logging(runtime)
    caplog.set_level(logging.INFO)
    log_event(
        logging.getLogger("dataagent.a2a"),
        "a2a.assembly.failed",
        secrets=("private-token",),
        request_id="report-id",
        root_run_id="run-id",
        task_id="task-id",
        message="private-token " + "x" * 4000,
        frames=[{"file": "private-token.py"}],
    )
    (record,) = [r for r in caplog.records if hasattr(r, "dataagent_event")]
    handler = configured["handlers"][0]
    try:
        payload = json.loads(handler.format(record))
    finally:
        for handler in configured["handlers"]:
            handler.close()
    assert payload["request_id"] == "report-id"
    assert payload["task_id"] == "task-id"
    assert len(payload["message"]) == 1500
    assert "private-token" not in caplog.text


async def test_a2a_sdk_errors_keep_protocol_envelopes_and_correlation(runtime):
    async with client_for(runtime, ScriptedModel(responses=[])) as client:
        payload = message("hello", "protocol-session")
        missing_version = await client.post("/a2a/jsonrpc", json=payload)
        assert "error" in missing_version.json()
        UUID(missing_version.headers["X-Request-ID"])
        for method, params, code in (
            ("UnknownMethod", {}, -32601),
            ("GetTask", {"id": "nonexistent-task"}, -32001),
            (
                "SendMessage",
                {
                    "message": {
                        "taskId": "nonexistent-task",
                        "messageId": "m",
                        "role": "ROLE_USER",
                        "parts": [{"text": "hello"}],
                    }
                },
                -32001,
            ),
        ):
            response = await rpc(
                client,
                {"jsonrpc": "2.0", "id": "protocol-query", "method": method, "params": params},
            )
            assert response.json()["error"]["code"] == code
            assert response.json()["id"] == "protocol-query"
            UUID(response.headers["X-Request-ID"])


async def test_a2a_custom_executor_metadata_does_not_break_response_audit(runtime):
    from a2a.helpers import new_task
    from a2a.types.a2a_pb2 import TaskState

    class CustomExecutor(AgentKitExecutor):
        async def execute(self, context, event_queue):
            task = new_task(context.task_id, context.context_id, TaskState.TASK_STATE_COMPLETED)
            task.metadata.update({"dataagent": "legacy-value"})
            await event_queue.enqueue_event(task)

    async with client_for(
        runtime, ScriptedModel(responses=[]), executor=CustomExecutor(None)
    ) as client:
        response = await rpc(client, message("hello", "custom-session"))
        assert task_of(response)["metadata"]["dataagent"] == "legacy-value"
        UUID(response.headers["X-Request-ID"])


async def test_a2a_cancel_completed_task_keeps_sdk_error_and_original_metadata(runtime):
    async with client_for(runtime, ScriptedModel(responses=[AIMessage(content="done")])) as client:
        completed = task_of(await rpc(client, message("hello", "completed-session")))
        response = await rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": "cancel",
                "method": "CancelTask",
                "params": {"id": completed["id"]},
            },
        )
        assert response.json()["error"]["code"] == -32002
        queried = await rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": "query",
                "method": "GetTask",
                "params": {"id": completed["id"]},
            },
        )
        assert queried.json()["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
        assert metadata(queried.json()["result"]) == metadata(completed)
