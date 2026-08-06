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
    RestApiLimits,
    SecurityLimitsMiddleware,
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
        assert resp.json()["status"] == "not_ready"
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
