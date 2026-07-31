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
"""REST ingress middleware: body limit, TTFB timeout, IP rate limit, concurrency queue."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Message, Receive, Scope, Send

from dataagent.config.config_manager import ConfigManager
from dataagent.utils.log import logger

request_id_var: ContextVar[str] = ContextVar("dataagent_request_id", default="")
_PROBE_PATHS = frozenset({"/health"})


@dataclass(frozen=True)
class RestApiLimits:
    """Ingress limits. ``request_timeout_seconds`` is TTFB only (not SSE body length)."""

    max_body_bytes: int = 1_048_576
    request_timeout_seconds: float = 120.0
    rate_limit_per_minute: int = 60
    max_concurrency: int = 16
    queue_timeout_seconds: float = 5.0


DEFAULT_REST_API_LIMITS = RestApiLimits()


def load_rest_api_limits(config_path: str | Path | None) -> RestApiLimits:
    """Load ``rest_api`` limits from a DataAgent YAML config file."""
    if not config_path:
        return DEFAULT_REST_API_LIMITS
    path = Path(config_path).expanduser()
    if not path.is_file():
        return DEFAULT_REST_API_LIMITS
    try:
        with path.open(encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        if not isinstance(cfg, dict):
            return DEFAULT_REST_API_LIMITS
        cfg = ConfigManager().interpolate_config(cfg)
        section = cfg.get("rest_api") or cfg.get("REST_API") or {}
        if not isinstance(section, dict):
            return DEFAULT_REST_API_LIMITS
        return RestApiLimits(
            max_body_bytes=_as_int(section.get("max_body_bytes"), DEFAULT_REST_API_LIMITS.max_body_bytes, minimum=1),
            request_timeout_seconds=_as_float(
                section.get("request_timeout_seconds"),
                DEFAULT_REST_API_LIMITS.request_timeout_seconds,
                minimum=0.001,
            ),
            rate_limit_per_minute=_as_int(
                section.get("rate_limit_per_minute"),
                DEFAULT_REST_API_LIMITS.rate_limit_per_minute,
                minimum=1,
            ),
            max_concurrency=_as_int(
                section.get("max_concurrency"),
                DEFAULT_REST_API_LIMITS.max_concurrency,
                minimum=1,
            ),
            queue_timeout_seconds=_as_float(
                section.get("queue_timeout_seconds"),
                DEFAULT_REST_API_LIMITS.queue_timeout_seconds,
                minimum=0.001,
            ),
        )
    except Exception as exc:
        logger.warning("Failed to load rest_api limits from {}: {}; using defaults.", path, exc)
        return DEFAULT_REST_API_LIMITS


def _as_int(value: Any, default: int, *, minimum: int | None = None) -> int:
    """Parse int from config; fall back to ``default`` on error or below ``minimum``."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


def _as_float(value: Any, default: float, *, minimum: float | None = None) -> float:
    """Parse float from config; fall back to ``default`` on error or below ``minimum``."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


def get_request_id() -> str:
    """Return the current request id from ContextVar (empty outside request scope)."""
    return request_id_var.get()


class SecurityLimitsMiddleware(BaseHTTPMiddleware):
    """P0 ingress limits + access audit.

    Semaphore is held until the response body finishes (SSE included).
    ``request_timeout_seconds`` is TTFB only.
    """

    def __init__(self, app: Any, limits: RestApiLimits):
        """Initialize semaphore, per-IP hit windows, and limit config."""
        super().__init__(app)
        self.limits = limits
        self._semaphore = asyncio.Semaphore(max(1, limits.max_concurrency))
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()
        self._hits_last_cleanup = 0.0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Wrap the ASGI ``receive`` callable for body metering (public ASGI pattern)."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        if path not in _PROBE_PATHS:
            receive = self._wrap_receive_with_body_limit(scope, receive)
        await super().__call__(scope, receive, send)

    async def dispatch(self, request: Request, call_next) -> Response:
        """Apply body/rate/concurrency limits, then forward with request id."""
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        client_ip = _client_ip(request)
        path = request.url.path
        is_probe = path in _PROBE_PATHS
        acquired = False
        try:
            if not is_probe:
                body_error = self._check_content_length(request)
                if body_error is not None:
                    body_error.headers["X-Request-Id"] = request_id
                    return body_error
                rate_error = await self._check_rate_limit(client_ip)
                if rate_error is not None:
                    rate_error.headers["X-Request-Id"] = request_id
                    return rate_error
                try:
                    await asyncio.wait_for(self._semaphore.acquire(), timeout=self.limits.queue_timeout_seconds)
                except TimeoutError:
                    return JSONResponse(
                        status_code=503,
                        content={"detail": "Server busy; concurrency queue timeout"},
                        headers={"X-Request-Id": request_id},
                    )
                acquired = True

            try:
                response = await asyncio.wait_for(call_next(request), timeout=self.limits.request_timeout_seconds)
            except TimeoutError:
                if acquired:
                    self._semaphore.release()
                    acquired = False
                response = JSONResponse(
                    status_code=504,
                    content={"detail": "Request timed out"},
                    headers={"X-Request-Id": request_id},
                )
                self._log_access(request_id, client_ip, path, response, started)
                return response

            if request.scope.get("dataagent_body_too_large"):
                if acquired:
                    self._semaphore.release()
                    acquired = False
                response = JSONResponse(
                    status_code=413,
                    content={"detail": f"Request body exceeds {self.limits.max_body_bytes} bytes"},
                    headers={"X-Request-Id": request_id},
                )
                self._log_access(request_id, client_ip, path, response, started)
                return response

            body_iterator = getattr(response, "body_iterator", None)
            if body_iterator is not None and acquired:
                response.body_iterator = self._hold_slot_until_body_ends(body_iterator)
                acquired = False
            elif acquired:
                self._semaphore.release()
                acquired = False

            response.headers["X-Request-Id"] = request_id
            self._log_access(request_id, client_ip, path, response, started)
            return response
        finally:
            if acquired:
                self._semaphore.release()
            request_id_var.reset(token)

    def _log_access(
        self,
        request_id: str,
        client_ip: str,
        path: str,
        response: Response,
        started: float,
    ) -> None:
        """Emit a structured rest.access audit log line."""
        logger.info(
            "rest.access request_id={} ip={} path={} status={} elapsed_ms={:.1f}",
            request_id,
            client_ip,
            path,
            getattr(response, "status_code", "-"),
            (time.perf_counter() - started) * 1000,
        )

    def _check_content_length(self, request: Request) -> Response | None:
        """Reject oversized or invalid Content-Length before reading the body."""
        max_bytes = self.limits.max_body_bytes
        content_length = request.headers.get("content-length")
        if content_length is None:
            return None
        try:
            if int(content_length) > max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"Request body exceeds {max_bytes} bytes"},
                )
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
        return None

    def _wrap_receive_with_body_limit(self, scope: Scope, receive: Receive) -> Receive:
        """Meter ``http.request`` bodies via the public ASGI receive callable."""
        max_bytes = self.limits.max_body_bytes
        received = 0
        scope["dataagent_body_too_large"] = False

        async def limited_receive() -> Message:
            nonlocal received
            if scope.get("dataagent_body_too_large"):
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"") or b""
                received += len(chunk)
                if received > max_bytes:
                    scope["dataagent_body_too_large"] = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        return limited_receive

    async def _hold_slot_until_body_ends(self, iterator: AsyncIterator[Any]) -> AsyncIterator[Any]:
        """Keep the concurrency slot until the response body completes."""
        aiter = iterator.__aiter__()
        try:
            async for chunk in aiter:
                yield chunk
        finally:
            close = getattr(aiter, "aclose", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    await close()
            self._semaphore.release()

    async def _check_rate_limit(self, client_ip: str) -> Response | None:
        """Return 429 if ``client_ip`` exceeds the per-minute hit window."""
        now = time.monotonic()
        window = 60.0
        limit = max(1, self.limits.rate_limit_per_minute)
        async with self._lock:
            if now - self._hits_last_cleanup >= window:
                stale = [ip for ip, hits in self._hits.items() if not hits or now - hits[-1] > window]
                for ip in stale:
                    self._hits.pop(ip, None)
                self._hits_last_cleanup = now
            hits = self._hits[client_ip]
            while hits and now - hits[0] > window:
                hits.popleft()
            if len(hits) >= limit:
                return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})
            hits.append(now)
        return None


def _client_ip(request: Request) -> str:
    """Best-effort client IP from X-Forwarded-For or the socket peer."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"
