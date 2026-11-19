"""Opt-in CLI/TCP acceptance with a real OpenAI-compatible model gateway."""

import json
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
import pytest
from test_a2a_e2e import backend
from test_a2a_server import message, task_of

pytestmark = pytest.mark.live


def wait_for(predicate, *, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.02)
    pytest.fail("Live A2A condition did not become true before its deadline")


@contextmanager
def observed_gateway(base_url):
    """Forward real responses; optional gates delay reading after the body was sent.

    No Authorization headers are retained. Gates make cancellation/queue/timeout
    independent of provider speed; they never manufacture model responses.
    """
    controls, calls = {}, []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            contents = [m.get("content") for m in json.loads(body)["messages"]]
            latest = next(c for c in reversed(contents) if isinstance(c, str))
            tag = re.search(r"A2ALIVE_[a-z0-9]+", latest).group()
            control = controls.setdefault(tag, {})
            record = {
                "tag": tag,
                "contents": contents,
                "expected_status": control.get("expected_status", 200),
                "sent": threading.Event(),
                "done": threading.Event(),
            }
            calls.append(record)

            def trace(event, _info):
                if event.endswith("send_request_body.complete"):
                    record["sent"].set()
                    if gate := control.get("release"):
                        if not gate.wait(30):
                            raise TimeoutError("Test gateway response gate expired")

            try:
                if barrier := control.get("barrier"):
                    barrier.wait(15)
                with httpx.Client(trust_env=False, timeout=60) as upstream:
                    response = upstream.post(
                        base_url.rstrip("/") + "/chat/completions",
                        content=body,
                        headers={
                            "Authorization": self.headers.get("Authorization", ""),
                            "Content-Type": "application/json",
                        },
                        extensions={"trace": trace},
                    )
                record["status"] = response.status_code
                if response.status_code == 200:
                    record["model"] = response.json().get("model")
                    record["usage"] = response.json().get("usage")
                data = response.content
                self.send_response(response.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Cancellation can close the downstream socket first.
            except httpx.HTTPError as error:
                # Keep the transport cause without retaining headers or exception reprs.
                record["transport_error"] = type(error).__name__
            finally:
                record["done"].set()

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", controls, calls
        finally:
            for control in controls.values():
                if gate := control.get("release"):
                    gate.set()
            for call in calls:
                call["done"].wait(65)
            server.shutdown()
            worker.join(5)


@pytest.fixture
def live_backend(runtime, tmp_path):
    required = (
        "DATAAGENT_A2A_LIVE_BASE_URL",
        "DATAAGENT_A2A_LIVE_API_KEY",
        "DATAAGENT_A2A_LIVE_MODEL",
    )
    if not all(os.environ.get(name) for name in required):
        pytest.skip("Set the three DATAAGENT_A2A_LIVE_* variables to enable real model calls")
    secret = os.environ[required[1]]
    report = []
    config_file = runtime.paths.home / "config.yaml"
    config = json.loads(config_file.read_text())
    with observed_gateway(os.environ[required[0]]) as (url, controls, calls):
        config["models"]["primary"].update(
            name=os.environ[required[2]],
            base_url=url,
            api_key="$env{DATAAGENT_A2A_LIVE_API_KEY}",
            max_retries=0,
            max_tokens=512,
        )
        config["dataagent"] = {"limits": {"timeout_seconds": 75}}
        config_file.write_text(json.dumps(config))
        yield runtime, config, controls, calls, report
    (tmp_path / "a2a-live-report.json").write_text(
        json.dumps(
            {
                "model": os.environ[required[2]],
                "cases": report,
                "upstream": [
                    {
                        k: c[k]
                        for k in ("tag", "status", "model", "usage", "transport_error")
                        if k in c
                    }
                    for c in calls
                ],
            },
            indent=2,
        )
    )
    # Avoid an assertion expression containing the credential: pytest may print operands.
    leaked = any(secret.encode() in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    assert not leaked, "Model credential appeared in test artifacts"
    for call in calls:
        assert call.get("status") == call["expected_status"], "Unexpected real gateway status"
        if call["status"] == 200:
            assert call["model"] == os.environ[required[2]]


def records(runtime):
    path = runtime.paths.state_dir / "logs/backend.log"
    if not path.exists():
        return []
    return [
        json.loads(line[line.index('{"event":') :])
        for line in path.read_text().splitlines()
        if '{"event":' in line
    ]


def send(client, text, context=None, *, immediate=False, tag=None):
    tag = tag or "A2ALIVE_" + uuid4().hex
    payload = message(f"{tag} {text} Do not use tools. Answer briefly.", context)
    if context is None:
        payload["params"]["message"].pop("contextId")
    if immediate:
        payload["params"]["configuration"] = {"returnImmediately": True}
    response = client.post("/a2a/jsonrpc", json=payload)
    task = task_of(response)
    assert response.json()["id"] == payload["id"]
    assert task["metadata"]["dataagent"]["requestId"] == response.headers["X-Request-ID"]
    assert task["metadata"]["dataagent"]["messageId"] == payload["params"]["message"]["messageId"]
    return task, tag


def query(client, method, task_id=None):
    response = client.post(
        "/a2a/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "id": str(uuid4()),
            "method": method,
            "params": {"id": task_id} if task_id else {},
        },
    )
    assert response.status_code == 200
    assert "error" not in response.json(), response.text
    return response, response.json()["result"]


def correlated(runtime, task, report, case, *, error=None, check_latest=True):
    details = task["metadata"]["dataagent"]
    expected = (
        "TASK_STATE_CANCELED"
        if error == "CANCELLED"
        else ("TASK_STATE_FAILED" if error else "TASK_STATE_COMPLETED")
    )
    assert task["status"]["state"] == expected, task
    if error:
        assert details["error"]["code"] == error
        assert details["error"]["stage"] == "agent_invoke"
    related = wait_for(
        lambda: [
            r
            for r in records(runtime)
            if r.get("task_id") == task["id"] and r["event"] == "a2a.trace.saved"
        ]
    )
    saved = related[-1]
    trace_path = runtime.paths.for_session(task["contextId"]).traces / (
        os.path.basename(saved["trace_file"])
    )
    spans = [
        s
        for r in json.loads(trace_path.read_text())["resourceSpans"]
        for scope in r["scopeSpans"]
        for s in scope["spans"]
    ]
    (root,) = [s for s in spans if not s.get("parentSpanId")]
    assert root["status"]["code"] == (2 if error else 1)
    if error:
        assert root["status"].get("message"), "Failed trace must retain its error detail"
    model_span = any(
        a["key"] == "gen_ai.request.model"
        and a["value"].get("stringValue") == os.environ["DATAAGENT_A2A_LIVE_MODEL"]
        for s in spans
        for a in s.get("attributes", [])
    )
    assert model_span
    if error in ("CANCELLED", "TIMEOUT"):
        interrupted = [s for s in spans
                       if "interrupted locally" in s.get("status", {}).get("message", "")]
        assert interrupted, "Interrupted model waits must remain visible in the trace"
        for span in interrupted:
            assert span["status"]["code"] == 2
            values = {a["key"]: next(iter(a["value"].values()))
                      for a in span.get("attributes", [])}
            assert json.loads(values.get("gen_ai.completion", "{}")) == {}
            assert not any(k.startswith("gen_ai.usage.") for k in values)
    attrs = {v["key"]: next(iter(v["value"].values())) for v in root["attributes"]}
    for key, value in (
        ("request_id", details["requestId"]),
        ("a2a_task_id", task["id"]),
        ("a2a_message_id", details["messageId"]),
        ("root_run_id", details["rootRunId"]),
        ("run_id", saved["native_run_id"]),
    ):
        assert attrs["langsmith.metadata.dataagent_" + key] == value
    assert attrs["langsmith.metadata.thread_id"] == task["contextId"]
    assert trace_path.stem == saved["native_run_id"]
    checkpoints = wait_for(
        lambda: [
            r
            for r in records(runtime)
            if r.get("task_id") == task["id"] and r["event"] == "a2a.checkpoint.saved"
        ]
    )
    checkpoint = checkpoints[-1]["checkpoint_id"]
    assert checkpoint
    with sqlite3.connect(runtime.paths.sessions_db) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM sessions WHERE thread_id = ?", (task["contextId"],)
        ).fetchone()
    if check_latest:
        assert row["last_run_id"] == details["rootRunId"]
        assert row["last_checkpoint_id"] == checkpoint
    event = "a2a.execution." + (
        "cancelled" if error == "CANCELLED" else ("failed" if error else "completed")
    )
    execution = wait_for(
        lambda: [
            r for r in records(runtime) if r.get("task_id") == task["id"] and r["event"] == event
        ]
    )[-1]
    for record in (saved, checkpoints[-1], execution):
        for key, value in (
            ("request_id", details["requestId"]),
            ("root_run_id", details["rootRunId"]),
            ("task_id", task["id"]),
            ("context_id", task["contextId"]),
            ("message_id", details["messageId"]),
        ):
            assert record[key] == value
    assert execution["rpc_id"] == "1"
    if error and error != "CANCELLED":
        assert execution["error_code"] == error
    report.append(
        {
            "case": case,
            "taskId": task["id"],
            "contextId": task["contextId"],
            **details,
            "trace": str(trace_path),
            "checkpointId": checkpoint,
            "modelSpanPresent": model_span,
        }
    )


def test_live_a2a_continuation_queue_parallel_and_cancel(live_backend, tmp_path):
    runtime, _config, controls, calls, report = live_backend
    with backend(runtime, tmp_path, request_timeout=90) as client:
        marker = "VALUE" + uuid4().hex[:12]
        first, first_tag = send(
            client, f"Conversation test value: {marker}. Reply exactly with that value."
        )
        correlated(runtime, first, report, "first_context_generated")
        followup = (
            "Read the previous user message in this conversation. Output its "
            "'Conversation test value' verbatim, without any A2ALIVE tag."
        )
        second, second_tag = send(client, followup, first["contextId"])
        upstream_second = next(c for c in calls if c["tag"] == second_tag)
        assert any(
            first_tag in c and marker in c
            for c in upstream_second["contents"]
            if isinstance(c, str)
        )
        assert marker in second["status"]["message"]["parts"][0]["text"]
        correlated(runtime, second, report, "continuation")
        for key in ("requestId", "messageId", "rootRunId"):
            assert first["metadata"]["dataagent"][key] != second["metadata"]["dataagent"][key]
        assert first["id"] != second["id"]
        response, queried = query(client, "GetTask", first["id"])
        assert queried["metadata"] == first["metadata"]
        assert response.headers["X-Request-ID"] != first["metadata"]["dataagent"]["requestId"]
        _, listed = query(client, "ListTasks")
        assert {first["id"], second["id"]} <= {t["id"] for t in listed["tasks"]}

        # Gate the first real request after upload; the second must stay in the queue.
        with ThreadPoolExecutor(max_workers=2) as pool:
            queued_context = "queue-" + uuid4().hex
            before = len(calls)
            # Pre-register the gate using a tag embedded in the latest prompt.
            tag = "A2ALIVE_" + uuid4().hex
            release = threading.Event()
            controls[tag] = {"release": release}
            payload = message(
                f"{tag} Conversation test value: {marker}. Reply exactly with that value. Do not use tools.",
                queued_context,
            )
            running = pool.submit(client.post, "/a2a/jsonrpc", json=payload)
            wait_for(lambda: len(calls) > before and calls[-1]["sent"].is_set())
            following = pool.submit(send, client, followup, queued_context)
            try:
                wait_for(
                    lambda: (
                        len(
                            [
                                r
                                for r in records(runtime)
                                if r.get("context_id") == queued_context
                                and r["event"] == "a2a.execution.queued"
                            ]
                        )
                        == 2
                    )
                )
                assert len(calls) == before + 1
                assert (
                    len(
                        [
                            r
                            for r in records(runtime)
                            if r.get("context_id") == queued_context
                            and r["event"] == "a2a.execution.started"
                        ]
                    )
                    == 1
                )
            finally:
                release.set()
            queued_first = task_of(running.result())
            correlated(runtime, queued_first, report, "same_context_first", check_latest=False)
            queued_second, second_tag = following.result()
            upstream_second = next(c for c in calls if c["tag"] == second_tag)
            assert payload["params"]["message"]["parts"][0]["text"] in upstream_second["contents"]
            assert marker in queued_second["status"]["message"]["parts"][0]["text"]
            correlated(runtime, queued_second, report, "same_context_serialized")
            checkpoints = [
                r
                for r in records(runtime)
                if r.get("context_id") == queued_context and r["event"] == "a2a.checkpoint.saved"
            ]
            assert len(checkpoints) == 2
            assert checkpoints[1]["checkpoint_id_before"] == checkpoints[0]["checkpoint_id"]
            assert checkpoints[0]["task_id"] == queued_first["id"]

        # A barrier at the real upstream boundary proves both contexts reach the model concurrently.
        barrier = threading.Barrier(2)
        payloads = []
        for _ in range(2):
            tag = "A2ALIVE_" + uuid4().hex
            controls[tag] = {"barrier": barrier}
            payloads.append(
                message(
                    f"{tag} Reply only with {tag}. Do not use tools.", "parallel-" + uuid4().hex
                )
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(client.post, "/a2a/jsonrpc", json=p) for p in payloads]
            for payload, future in zip(payloads, futures, strict=True):
                task = task_of(future.result())
                own_tag = payload["params"]["message"]["parts"][0]["text"].split()[0]
                assert own_tag in task["status"]["message"]["parts"][0]["text"]
                model_input = next(c for c in calls if c["tag"] == own_tag)["contents"]
                other = next(p for p in payloads if p is not payload)
                other_tag = other["params"]["message"]["parts"][0]["text"].split()[0]
                assert not any(other_tag in c for c in model_input if isinstance(c, str))
                correlated(runtime, task, report, "distinct_context_parallel")

        cancel_tag = "A2ALIVE_" + uuid4().hex
        release = threading.Event()
        controls[cancel_tag] = {"release": release}
        submitted, _ = send(
            client, "Reply with OK.", "cancel-" + uuid4().hex, immediate=True, tag=cancel_tag
        )
        try:
            wait_for(lambda: any(c["tag"] == cancel_tag and c["sent"].is_set() for c in calls))
            response, canceled = query(client, "CancelTask", submitted["id"])
            correlated(runtime, canceled, report, "cancel_inflight_model", error="CANCELLED")
            assert (
                canceled["metadata"]["dataagent"]["requestId"]
                == (submitted["metadata"]["dataagent"]["requestId"])
            )
            assert (
                response.headers["X-Request-ID"] != canceled["metadata"]["dataagent"]["requestId"]
            )
            assert not any(
                r.get("task_id") == canceled["id"] and r["event"] == "a2a.execution.completed"
                for r in records(runtime)
            )
            (cancel_record,) = [
                r
                for r in records(runtime)
                if r["event"] == "a2a.task.cancel_requested" and r["task_id"] == canceled["id"]
            ]
            assert cancel_record["request_id"] == response.headers["X-Request-ID"]
            assert cancel_record["root_run_id"] == canceled["metadata"]["dataagent"]["rootRunId"]
        finally:
            release.set()
        resumed, _ = send(client, "Reply only with RECOVERED.", submitted["contextId"])
        assert "RECOVERED" in resumed["status"]["message"]["parts"][0]["text"]
        correlated(runtime, resumed, report, "continue_after_cancel")

        count = len(calls)
        response = client.post("/a2a/jsonrpc", json=message(" ", "blank-live"))
        failed = task_of(response)
        assert failed["metadata"]["dataagent"]["requestId"] == response.headers["X-Request-ID"]
        assert failed["metadata"]["dataagent"]["error"]["code"] == "BAD_REQUEST"
        assert "rootRunId" not in failed["metadata"]["dataagent"]
        assert len(calls) == count
        report.append({"case": "validation_no_model_call", **failed["metadata"]["dataagent"]})


@pytest.mark.parametrize("failure", ["timeout", "upstream_auth"])
def test_live_a2a_error_codes_and_correlation(live_backend, tmp_path, monkeypatch, failure):
    runtime, config, controls, calls, report = live_backend
    tag = "A2ALIVE_" + uuid4().hex
    release = threading.Event()
    if failure == "timeout":
        config["dataagent"]["limits"]["timeout_seconds"] = 3
        controls[tag] = {"release": release}
        (runtime.paths.home / "config.yaml").write_text(json.dumps(config))
    else:
        # A real gateway rejection, distinct from the anonymous A2A service's auth.
        controls[tag] = {"expected_status": 401}
        monkeypatch.setenv("DATAAGENT_A2A_LIVE_API_KEY", "invalid-live-test-key")
    with backend(runtime, tmp_path, request_timeout=90) as client:
        failed, _ = send(client, "Reply only with OK.", "failure-" + uuid4().hex, tag=tag)
        try:
            expected = "TIMEOUT" if failure == "timeout" else "MODEL_ERROR"
            assert any(c["sent"].is_set() for c in calls)
            correlated(runtime, failed, report, failure, error=expected)
            response, queried = query(client, "GetTask", failed["id"])
            assert queried["metadata"] == failed["metadata"]
            assert response.headers["X-Request-ID"] != failed["metadata"]["dataagent"]["requestId"]
            if failure == "upstream_auth":
                assert calls[-1]["status"] == 401
        finally:
            release.set()
