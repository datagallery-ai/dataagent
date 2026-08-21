# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from dataagent.interface.rest_api.middleware import (
    DEFAULT_REST_API_LIMITS,
    RestApiLimits,
    SecurityLimitsMiddleware,
    _client_ip,
    load_rest_api_limits,
)
from dataagent.interface.rest_api.service import DataAgentService


async def _ok(request: Request) -> JSONResponse:
    if request.method in {"POST", "PUT", "PATCH"}:
        await request.body()
    return JSONResponse({"ok": True})


def _app(limits: RestApiLimits) -> Starlette:
    app = Starlette(routes=[Route("/api/agent/query", _ok, methods=["POST"]), Route("/health", _ok)])
    app.add_middleware(SecurityLimitsMiddleware, limits=limits)
    return app


def test_load_rest_api_limits_from_yaml(tmp_path: Path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "rest_api:\n  max_body_bytes: 100\n  rate_limit_per_minute: 2\n  max_concurrency: 1\n",
        encoding="utf-8",
    )
    limits = load_rest_api_limits(cfg)
    assert limits.max_body_bytes == 100
    assert limits.rate_limit_per_minute == 2
    assert limits.max_concurrency == 1


def test_load_rest_api_limits_rejects_non_positive_values(tmp_path: Path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "rest_api:\n"
        "  max_body_bytes: -1\n"
        "  request_timeout_seconds: -5\n"
        "  queue_timeout_seconds: 0\n"
        "  rate_limit_per_minute: -3\n"
        "  max_concurrency: 0\n",
        encoding="utf-8",
    )
    limits = load_rest_api_limits(cfg)
    assert limits.max_body_bytes == RestApiLimits.max_body_bytes
    assert limits.request_timeout_seconds == RestApiLimits.request_timeout_seconds
    assert limits.queue_timeout_seconds == RestApiLimits.queue_timeout_seconds
    assert limits.rate_limit_per_minute == RestApiLimits.rate_limit_per_minute
    assert limits.max_concurrency == RestApiLimits.max_concurrency


def test_middleware_rejects_oversized_body_and_rate_limits():
    client = TestClient(_app(RestApiLimits(max_body_bytes=10, rate_limit_per_minute=1)))
    oversized = client.post("/api/agent/query", content=b"x" * 20, headers={"content-length": "20"})
    assert oversized.status_code == 413
    assert oversized.headers.get("x-request-id")

    client = TestClient(_app(RestApiLimits(rate_limit_per_minute=1, max_body_bytes=1_000_000)))
    assert client.post("/api/agent/query", json={"q": 1}).status_code == 200
    limited = client.post("/api/agent/query", json={"q": 2})
    assert limited.status_code == 429
    assert limited.headers.get("x-request-id")


def _http_request(*, client: tuple[str, int] | None, forwarded_for: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": headers,
            "client": client,
            "server": ("test", 80),
        }
    )


def test_client_ip_uses_socket_peer_when_xff_absent():
    request = _http_request(client=("10.0.0.8", 123))
    assert _client_ip(request) == "10.0.0.8"


def test_client_ip_ignores_forged_x_forwarded_for():
    request = _http_request(client=("10.0.0.8", 123), forwarded_for="203.0.113.1, 192.0.2.1")
    assert _client_ip(request) == "10.0.0.8"


def test_client_ip_unknown_when_peer_missing_even_with_xff():
    request = _http_request(client=None, forwarded_for="203.0.113.1")
    assert _client_ip(request) == "unknown"


def test_rate_limit_key_ignores_forged_x_forwarded_for():
    client = TestClient(_app(RestApiLimits(rate_limit_per_minute=1, max_body_bytes=1_000_000)))
    first = client.post("/api/agent/query", json={"q": 1}, headers={"X-Forwarded-For": "203.0.113.1"})
    second = client.post("/api/agent/query", json={"q": 2}, headers={"X-Forwarded-For": "198.51.100.2"})
    assert first.status_code == 200
    assert second.status_code == 429


@pytest.mark.asyncio
async def test_middleware_meters_body_without_or_understated_content_length():
    """Missing / understated Content-Length must still 413 (DoS bypass)."""
    limits = RestApiLimits(max_body_bytes=10, rate_limit_per_minute=10_000, max_concurrency=8)
    body = b"x" * 20

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    for headers in (
        [(b"host", b"test")],
        [(b"host", b"test"), (b"content-length", b"5")],
    ):
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/agent/query",
            "raw_path": b"/api/agent/query",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 123),
            "server": ("test", 80),
        }
        messages: list[dict] = []

        async def send(message, _messages=messages):
            _messages.append(message)

        await _app(limits)(scope, receive, send)
        start = next(m for m in messages if m["type"] == "http.response.start")
        assert start["status"] == 413
        messages.clear()


@pytest.mark.asyncio
async def test_streaming_holds_semaphore_until_body_ends():
    stream_current = 0
    stream_peak = 0
    lock = asyncio.Lock()

    async def slow_stream(_request: Request) -> StreamingResponse:
        async def gen():
            nonlocal stream_current, stream_peak
            async with lock:
                stream_current += 1
                stream_peak = max(stream_peak, stream_current)
            try:
                await asyncio.sleep(0.15)
                yield b"data: chunk\n\n"
                await asyncio.sleep(0.05)
                yield b"data: done\n\n"
            finally:
                async with lock:
                    stream_current -= 1

        return StreamingResponse(gen(), media_type="text/event-stream")

    inner = Starlette(routes=[Route("/stream", slow_stream, methods=["GET"])])
    limits = RestApiLimits(
        max_concurrency=1,
        request_timeout_seconds=5.0,
        rate_limit_per_minute=10_000,
        queue_timeout_seconds=2.0,
        max_body_bytes=1_000_000,
    )
    app = SecurityLimitsMiddleware(inner, limits=limits)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:

        async def one():
            resp = await client.get("/stream", timeout=5.0)
            _ = resp.content
            return resp.status_code

        t0 = time.perf_counter()
        results = await asyncio.gather(one(), one())
        elapsed = time.perf_counter() - t0

    assert results == [200, 200]
    assert stream_peak == 1
    assert elapsed >= 0.35


@pytest.mark.asyncio
async def test_probes_bypass_concurrency_during_streaming():
    release_stream = asyncio.Event()

    async def slow_stream(_request: Request) -> StreamingResponse:
        async def gen():
            yield b"data: start\n\n"
            await release_stream.wait()
            yield b"data: done\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def probe(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    inner = Starlette(
        routes=[
            Route("/stream", slow_stream, methods=["GET"]),
            Route("/health", probe, methods=["GET"]),
        ]
    )
    limits = RestApiLimits(
        max_concurrency=1,
        request_timeout_seconds=5.0,
        rate_limit_per_minute=10_000,
        queue_timeout_seconds=0.05,
        max_body_bytes=1_000_000,
    )
    app = SecurityLimitsMiddleware(inner, limits=limits)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        stream_task = asyncio.create_task(client.get("/stream", timeout=5.0))
        await asyncio.sleep(0.05)
        health = await client.get("/health", timeout=1.0)
        release_stream.set()
        stream_resp = await stream_task

    assert health.status_code == 200
    assert stream_resp.status_code == 200


@pytest.mark.asyncio
async def test_request_timeout_cancels_downstream_task():
    """A 504 response must cancel the downstream request before the middleware exits."""
    downstream_started = asyncio.Event()
    downstream_stopped = asyncio.Event()
    response_started = asyncio.Event()
    never_finish = asyncio.Event()

    async def slow_app(scope, receive, send):
        _ = scope, receive, send
        downstream_started.set()
        try:
            await never_finish.wait()
        finally:
            downstream_stopped.set()

    limits = RestApiLimits(
        max_concurrency=1,
        request_timeout_seconds=0.02,
        rate_limit_per_minute=10_000,
        queue_timeout_seconds=0.1,
        max_body_bytes=1_000_000,
    )
    app = SecurityLimitsMiddleware(slow_app, limits=limits)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/agent/query",
        "raw_path": b"/api/agent/query",
        "query_string": b"",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
    }
    messages: list[dict] = []

    async def receive():
        await never_finish.wait()

    async def send(message):
        messages.append(message)
        if message.get("type") == "http.response.start":
            response_started.set()

    app_task = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(downstream_started.wait(), timeout=0.1)
        await asyncio.wait_for(response_started.wait(), timeout=0.1)
        start = next(message for message in messages if message.get("type") == "http.response.start")
        assert start.get("status") == 504
        await asyncio.wait_for(downstream_stopped.wait(), timeout=0.1)
        await asyncio.wait_for(app_task, timeout=0.1)
    finally:
        if not app_task.done():
            app_task.cancel()
            await asyncio.gather(app_task, return_exceptions=True)


def test_default_request_timeout_seconds_is_600():
    """Ingress default covers the whole request, including SSE body."""
    assert RestApiLimits.request_timeout_seconds == 600.0
    assert DEFAULT_REST_API_LIMITS.request_timeout_seconds == 600.0
    assert load_rest_api_limits(None).request_timeout_seconds == 600.0


def test_default_queue_timeout_seconds_is_15():
    assert RestApiLimits.queue_timeout_seconds == 15.0
    assert DEFAULT_REST_API_LIMITS.queue_timeout_seconds == 15.0
    assert load_rest_api_limits(None).queue_timeout_seconds == 15.0


@pytest.mark.asyncio
async def test_nonstreaming_timeout_returns_504():
    """A handler that never returns still maps to 504 after request_timeout_seconds."""

    async def slow(_request: Request) -> JSONResponse:
        await asyncio.sleep(1.0)
        return JSONResponse({"ok": True})

    inner = Starlette(routes=[Route("/slow", slow, methods=["GET"])])
    limits = RestApiLimits(
        max_concurrency=1,
        request_timeout_seconds=0.05,
        rate_limit_per_minute=10_000,
        queue_timeout_seconds=0.5,
        max_body_bytes=1_000_000,
    )
    app = SecurityLimitsMiddleware(inner, limits=limits)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/slow", timeout=2.0)
    assert resp.status_code == 504
    assert resp.json()["detail"] == "Request timed out"


@pytest.mark.asyncio
async def test_streaming_total_timeout_ends_body_releases_slot_and_cancels_downstream():
    """Never-ending SSE must stop at request_timeout_seconds, free the slot, and cancel downstream."""
    downstream_stopped = asyncio.Event()
    never_finish = asyncio.Event()

    async def never_ending_stream(_request: Request) -> StreamingResponse:
        async def gen():
            try:
                yield b"data: start\n\n"
                await never_finish.wait()
                yield b"data: done\n\n"
            finally:
                downstream_stopped.set()

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def ok(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    inner = Starlette(
        routes=[
            Route("/stream", never_ending_stream, methods=["GET"]),
            Route("/ok", ok, methods=["GET"]),
        ]
    )
    limits = RestApiLimits(
        max_concurrency=1,
        request_timeout_seconds=0.05,
        rate_limit_per_minute=10_000,
        queue_timeout_seconds=0.2,
        max_body_bytes=1_000_000,
    )
    app = SecurityLimitsMiddleware(inner, limits=limits)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        t0 = time.perf_counter()
        try:
            resp = await asyncio.wait_for(client.get("/stream", timeout=5.0), timeout=0.8)
        except TimeoutError:
            pytest.fail("streaming request did not finish after request_timeout_seconds")
        elapsed = time.perf_counter() - t0
        content = resp.content
        second = await client.get("/ok", timeout=1.0)

    assert resp.status_code == 200
    assert elapsed < 0.8
    assert b"data: done" not in content
    assert second.status_code == 200
    await asyncio.wait_for(downstream_stopped.wait(), timeout=0.5)


def test_service_is_ready_false_until_initialized():
    assert DataAgentService().is_ready() is False


@pytest.mark.asyncio
async def test_health_returns_503_when_service_not_ready():
    """``/health`` is the availability probe: 503 until agent is initialized."""
    from dataagent.interface.rest_api import app as rest_app

    previous = rest_app._data_agent_service
    rest_app._data_agent_service = DataAgentService()
    try:
        transport = ASGITransport(app=rest_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health", timeout=1.0)
        assert resp.status_code == 503
        assert resp.json() == {"status": "not_ready"}
    finally:
        rest_app._data_agent_service = previous


class _ReadyService:
    def is_ready(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_health_returns_ok_without_component_name():
    """Probe body is status only; do not echo the product name."""
    from dataagent.interface.rest_api import app as rest_app

    previous = rest_app._data_agent_service
    rest_app._data_agent_service = _ReadyService()
    try:
        transport = ASGITransport(app=rest_app.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health", timeout=1.0)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
    finally:
        rest_app._data_agent_service = previous


def test_format_nl2sql_structured_candidates():
    import hashlib
    import re

    service = DataAgentService()
    service._cached_agent_type = "nl2sql"
    result = service._format_result(
        {
            "sql": "SELECT 1",
            "confidence": 0.9,
            "generation_results": [{"id": 0, "sql": "SELECT 1", "prompt": "SECRET"}],
        }
    )
    payload = result["result"]
    assert payload["success"] is True
    assert payload["candidates"][0]["sql"] == "SELECT 1"
    expected = hashlib.sha256(re.sub(r"\s+", " ", "SELECT 1".strip()).encode()).hexdigest()
    assert payload["sql_fingerprint"] == expected
    assert "SECRET" not in str(result)


def test_format_nl2sql_omits_fingerprint_when_sql_empty():
    service = DataAgentService()
    service._cached_agent_type = "nl2sql"
    result = service._format_result({"sql": "", "trace_id": "abc"})
    payload = result["result"]
    assert payload["sql"] == ""
    assert "sql_fingerprint" not in payload
