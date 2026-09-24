"""Native SDK retries against a local HTTP peer, never a real model provider."""

import json
import socket
import threading
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_api import client_for, query, sse, terminal
from test_launch import MODEL, config

from dataagent import LaunchOptions, prepare_runtime
from dataagent.agent import build_model


def tool_delta(name, arguments):
    return {"role": "assistant", "tool_calls": [{
        "index": 0, "id": f"call_{name}", "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }]}


@contextmanager
def provider(*outcomes, retry_after="0.001"):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            action = outcomes[min(len(requests), len(outcomes) - 1)]
            requests.append((body, self.headers.get("x-stainless-retry-count")))
            if action == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                return
            if isinstance(action, int):
                data = b'{"error":{"message":"synthetic provider error","type":"server_error"}}'
                self.send_response(action)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", retry_after)
            else:
                delta = action if isinstance(action, dict) else {"role": "assistant", "content": action}
                chunks = [{"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1,
                           "model": "test-model", "choices": [{"index": 0, "delta": value,
                           "finish_reason": finish}]} for value, finish in (
                               (delta, None), ({}, "tool_calls" if "tool_calls" in delta else "stop"),
                           )]
                if action == "partial":
                    chunks = chunks[:1]
                data = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks).encode()
                if action != "partial":
                    data += b"data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data) + (100 if action == "partial" else 0)))
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
            if action == "partial":
                self.close_connection = True  # Truncated HTTP body after a real SSE token.

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", requests
        finally:
            server.shutdown()
            worker.join(timeout=5)


@pytest.fixture
async def models(runtime):
    created = []

    def create(url, max_retries=2):
        selected = runtime.settings.dataagent.model.default
        settings = runtime.settings.model_copy(update={"models": {
            **runtime.settings.models,
            selected: runtime.settings.models[selected].model_copy(update={
                "base_url": url, "max_retries": max_retries,
            }),
        }})
        model = build_model(settings)
        created.append(model)
        assert model.max_retries == model.root_client.max_retries == model.root_async_client.max_retries == max_retries
        assert model.use_responses_api is False
        return model

    yield create
    for model in created:
        await model.root_async_client.close()
        model.root_client.close()


def test_retry_config_defaults_and_layer_override(tmp_path):
    home = tmp_path / "home"
    config(home / "config.json", MODEL)
    assert prepare_runtime().settings.models["primary"].max_retries == 2
    config(home / "config.json", {"models": {"primary": {**MODEL["models"]["primary"], "max_retries": 4}}})
    override = config(tmp_path / "override.json", {"models": {"primary": {"max_retries": 0}}})
    assert prepare_runtime().settings.models["primary"].max_retries == 4
    assert prepare_runtime(LaunchOptions(config=override)).settings.models["primary"].max_retries == 0


@pytest.mark.parametrize("value", [-1, 1.5, True, "2", None])
def test_invalid_retry_config_is_rejected_before_model_creation(tmp_path, value):
    path = config(tmp_path / "invalid.json", {"models": {"primary": {
        **MODEL["models"]["primary"], "max_retries": value,
    }}})
    with pytest.raises(ValueError, match="max_retries"):
        prepare_runtime(LaunchOptions(config=path))


@pytest.mark.parametrize("failure", ["disconnect", 408, 409, 429, 500, 503])
async def test_native_transient_request_retry_then_success(runtime, models, failure):
    with provider(failure, "recovered") as (url, requests):
        async with client_for(runtime, models(url)) as (client, _):
            events = sse(await client.post("/dataagent/stream", json=query()))
    assert terminal(events) == ["RUN_FINISHED"]
    assert [retry for _, retry in requests] == ["0", "1"]
    assert requests[0][0] == requests[1][0]


@pytest.mark.parametrize("status,retries,attempts", [(503, 2, 3), (503, 0, 1), (400, 2, 1), (401, 2, 1)])
async def test_exhaustion_disabled_retries_and_nonretryable_errors(runtime, models, status, retries, attempts):
    with provider(status) as (url, requests):
        async with client_for(runtime, models(url, retries)) as (client, _):
            events = sse(await client.post("/dataagent/stream", json=query()))
    assert terminal(events) == ["RUN_ERROR"]
    assert len(requests) == attempts


async def test_retry_in_subagent_does_not_replay_tool_or_agent(runtime, models, caplog):
    caplog.set_level("INFO", logger="dataagent.audit")
    with provider(
        tool_delta("task", {"subagent_type": "general-purpose", "description": "Write result.txt"}),
        tool_delta("write_file", {"file_path": str(runtime.paths.for_session("thread-1").outputs / "result.txt"),
                                  "content": "written once"}),
        503, "Child finished.", "Root finished.",
    ) as (url, requests):
        async with client_for(runtime, models(url)) as (client, _):
            events = sse(await client.post("/dataagent/stream", json=query()))
    assert terminal(events) == ["RUN_FINISHED"]
    assert [retry for _, retry in requests] == ["0", "0", "0", "1", "0"]
    assert requests[2][0] == requests[3][0]
    assert caplog.text.count("hook.before_agent agent=general-purpose") == 1
    assert caplog.text.count("hook.before_tool agent=general-purpose tool=write_file") == 1
    assert (runtime.paths.for_session("thread-1").outputs / "result.txt").read_text() == "written once"


async def test_partial_stream_is_not_retried_or_duplicated(runtime, models):
    with provider("partial") as (url, requests):
        async with client_for(runtime, models(url)) as (client, _):
            events = sse(await client.post("/dataagent/stream", json=query()))
    assert terminal(events) == ["RUN_ERROR"]
    assert len(requests) == 1
    assert "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT") == "partial"


async def test_whole_run_timeout_includes_native_retry_backoff(runtime, models):
    runtime = replace(runtime, settings=runtime.settings.model_copy(update={
        "dataagent": runtime.settings.dataagent.model_copy(update={
            "limits": runtime.settings.dataagent.limits.model_copy(update={"timeout_seconds": 0.2}),
        }),
    }))
    with provider(503, retry_after="30") as (url, requests):
        async with client_for(runtime, models(url)) as (client, _):
            events = sse(await client.post("/dataagent/stream", json=query()))
    assert terminal(events) == ["RUN_ERROR"]
    assert events[-1]["code"] == "TimeoutError"
    assert len(requests) == 1
