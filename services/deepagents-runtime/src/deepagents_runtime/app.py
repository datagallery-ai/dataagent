from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from deepagents_runtime.agent import create_runtime_agent
from deepagents_runtime.config import RuntimeSettings
from deepagents_runtime.events import CONTRACT_VERSION, PROVIDER, encode_sse
from deepagents_runtime.models import CancelRequest, RuntimeHealth, RuntimeRunRequest
from deepagents_runtime.stream import iter_runtime_events


class RunRegistry:
    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}

    def begin(self, run_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self._events[run_id] = event
        return event

    def cancel(self, run_id: str) -> bool:
        event = self._events.get(run_id)
        if event is None:
            return False
        event.set()
        return True

    def end(self, run_id: str) -> None:
        self._events.pop(run_id, None)


def create_app(
    settings: RuntimeSettings | None = None,
    *,
    agent: Any = None,
    model: Any = None,
) -> FastAPI:
    resolved = settings or RuntimeSettings.from_env()
    registry = RunRegistry()

    app = FastAPI(title="DataFoundry Deep Agents Runtime", version=CONTRACT_VERSION)
    app.state.settings = resolved
    app.state.agent = agent
    app.state.model = model
    app.state.registry = registry

    def ensure_agent() -> Any:
        if app.state.agent is None:
            app.state.agent = create_runtime_agent(resolved, model=app.state.model)
        return app.state.agent

    @app.middleware("http")
    async def check_token(request: Request, call_next):  # type: ignore[no-untyped-def]
        token = resolved.token
        if token and request.headers.get("authorization") != f"Bearer {token}":
            return JSONResponse({"error": "UNAUTHORIZED"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    async def health() -> RuntimeHealth:
        try:
            ready = ensure_agent() is not None
        except Exception:
            ready = False
        return RuntimeHealth(
            status="ok" if ready else "degraded",
            provider=PROVIDER,
            version=CONTRACT_VERSION,
            capabilities={
                "streaming": True,
                "tools": True,
                "interrupt": True,
                "cancel": True,
            },
        )

    @app.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, body: CancelRequest | None = None) -> dict[str, Any]:
        canceled = registry.cancel(run_id)
        return {"canceled": canceled, "reason": (body.reason if body else "RUN_CANCELLED")}

    @app.post("/runs/stream")
    async def stream_run(request: RuntimeRunRequest) -> StreamingResponse:
        runtime_agent = ensure_agent()
        cancel_event = registry.begin(request.runId)

        async def generate() -> AsyncIterator[bytes]:
            try:
                async for event in iter_runtime_events(runtime_agent, request, cancelled=cancel_event):
                    yield encode_sse(event).encode("utf-8")
                    if event.get("type") in {"RUN_FINISHED", "RUN_ERROR"} or event.get("name") == "on_interrupt":
                        return
            finally:
                registry.end(request.runId)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


app = create_app()
