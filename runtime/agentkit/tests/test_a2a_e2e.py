"""Real CLI/TCP/checkpoint/logging acceptance against a synthetic model provider."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from process_helpers import stop_child
from test_a2a_server import message


@contextmanager
def model_provider():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            failed = len(requests) == 3
            payload = (
                {"error": {"message": "synthetic model failure", "type": "server_error"}}
                if failed
                else {
                    "id": "completion-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": f"answer-{len(requests)}"},
                        }
                    ],
                }
            )
            data = json.dumps(payload).encode()
            self.send_response(500 if failed else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
        )
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", requests
        finally:
            server.shutdown()
            worker.join(5)


@contextmanager
def backend(runtime, tmp_path, *, request_timeout=20):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = dict(os.environ)
    for key in (
        "DATAAGENT_A2A_AUTH_TOKEN",
        "DATAAGENT_OTLP_TRACES_ENDPOINT",
        "DATAAGENT_OTLP_HEADERS",
    ):
        env.pop(key, None)
    env["DATAAGENT_HOME"] = str(runtime.paths.home)
    with (tmp_path / "backend.stderr.log").open("a") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "serve",
                "serve-a2a",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=tmp_path,
            env=env,
            stdout=output,
            stderr=output,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                trust_env=False,
                timeout=request_timeout,
                headers={"A2A-Version": "1.0"},
            ) as client:
                deadline = time.monotonic() + 20
                while True:
                    assert process.poll() is None, (
                        "A2A backend exited before discovery was available"
                    )
                    try:
                        if (
                            client.get("/.well-known/agent-card.json", timeout=0.5).status_code
                            == 200
                        ):
                            break
                    except httpx.RequestError:
                        pass
                    assert time.monotonic() < deadline, "A2A startup timed out"
                    time.sleep(0.02)
                yield client
        finally:
            stop_child(process)


def test_a2a_cli_restart_keeps_history_and_operator_correlation(runtime, tmp_path):
    config_file = runtime.paths.home / "config.yaml"
    config = json.loads(config_file.read_text())
    with model_provider() as (url, requests):
        config["models"]["primary"].update(base_url=url, max_retries=0)
        config_file.write_text(json.dumps(config))
        with backend(runtime, tmp_path) as client:
            first_response = client.post(
                "/a2a/jsonrpc", json=message("remember CODE-42", "restart-session")
            )
            first = first_response.json()["result"]["task"]
            assert first["status"]["state"] == "TASK_STATE_COMPLETED"
            first_details = first["metadata"]["dataagent"]
            assert first_details["requestId"] == first_response.headers["X-Request-ID"]
            assert client.get("/api/v1/sessions").status_code == 404
        with backend(runtime, tmp_path) as client:
            old = client.post(
                "/a2a/jsonrpc",
                json={
                    "jsonrpc": "2.0",
                    "id": "query",
                    "method": "GetTask",
                    "params": {"id": first["id"]},
                },
            )
            assert old.json()["error"]["code"] == -32001
            second_response = client.post(
                "/a2a/jsonrpc", json=message("continue", "restart-session")
            )
            second = second_response.json()["result"]["task"]
            assert second["status"]["state"] == "TASK_STATE_COMPLETED"
            assert second["metadata"]["dataagent"]["rootRunId"] != first_details["rootRunId"]
            contents = [m.get("content") for m in requests[1]["messages"]]
            assert (
                "remember CODE-42" in contents and "answer-1" in contents and "continue" in contents
            )
            failed = client.post("/a2a/jsonrpc", json=message("fail", "restart-session")).json()[
                "result"
            ]["task"]
            assert failed["metadata"]["dataagent"]["error"]["code"] == "MODEL_ERROR"
        text = (runtime.paths.state_dir / "logs/backend.log").read_text()
        records = [json.loads(line) for line in text.splitlines()]
        for task in (first, second, failed):
            related = [r for r in records if r.get("task_id") == task["id"]]
            assert any(r["event"] == "a2a.trace.saved" for r in related)
            assert any(
                r.get("root_run_id") == task["metadata"]["dataagent"]["rootRunId"] for r in related
            )
        assert "test-secret" not in text
