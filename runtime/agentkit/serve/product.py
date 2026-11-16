"""Product routes mounted on the same process as the conversation engine."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from serve.envelopes import error, success


def is_conversation_path(path: str) -> bool:
    """True for agent routes that require a live engine.

    The Agent Card is static discovery data, so it stays available while the engine starts.
    """
    if path in {"/api/v1/runs", "/api/v1/sessions", "/a2a/jsonrpc"}:
        return True
    return path.startswith(("/api/v1/runs/", "/api/v1/sessions/"))


def conversation_status(app: FastAPI) -> dict:
    """Report whether the in-process engine is ready."""
    runtime = getattr(getattr(app.state, "agents", None), "runtime", None)
    if runtime is None:
        return {"ready": False}
    from serve.app import agentkit_health_body

    instance_id = getattr(app.state, "conversation_instance_id", "")
    return {"ready": True, **agentkit_health_body(runtime, instance_id)}


def register_product_routes(app: FastAPI) -> None:
    """Register /healthz, /ready, and /api/v1 routes. Conversation routes stay separate."""

    @app.get("/healthz")
    @app.head("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return success({"status": "ok", "conversation": conversation_status(app)})

    @app.get("/ready")
    @app.head("/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        conversation = conversation_status(app)
        if not conversation.get("ready"):
            return error("CONVERSATION_UNAVAILABLE", "Conversation engine is not ready.", 503)
        return success({"status": "ready", "conversation": conversation})

    @app.get("/api/v1/me")
    async def me() -> JSONResponse:
        user_id = _local_user_id(app)
        return success({
            "user": {"id": user_id, "displayName": user_id},
            "workspace": {"id": "local", "name": "Local"},
        })

    @app.post("/api/v1/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> Response:
        from serve.app import cancel_active_run

        if not cancel_active_run(app, run_id):
            return error("RESOURCE_NOT_FOUND", "Run is not active.", 404)
        return Response(status_code=204)


class ConversationReadyGuard:
    """Return 503 for conversation routes until the in-process engine is ready."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_conversation_path(scope.get("path") or ""):
            fastapi_app = scope.get("app")
            agents = getattr(getattr(fastapi_app, "state", None), "agents", None)
            if agents is None:
                await error("CONVERSATION_UNAVAILABLE", "Conversation engine is not ready.", 503)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _local_user_id(app: FastAPI) -> str:
    runtime = getattr(getattr(app.state, "agents", None), "runtime", None) or getattr(
        app.state, "conversation_runtime", None
    )
    return str(getattr(getattr(runtime, "paths", None), "user_id", None) or "default")

