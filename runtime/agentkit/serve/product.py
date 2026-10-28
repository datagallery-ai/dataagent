"""Product routes mounted on the same process as the conversation engine."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from serve.envelopes import error, success


def is_conversation_path(path: str) -> bool:
    """True for agent routes that require a live engine."""
    if path in {
        "/dataagent/stream",
        "/api/copilotkit",
        "/sessions",
        "/.well-known/agent-card.json",
        "/a2a/jsonrpc",
    }:
        return True
    return path.startswith(("/dataagent/stream/", "/api/copilotkit/", "/sessions/"))


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
    async def healthz() -> JSONResponse:
        return success({"status": "ok", "conversation": conversation_status(app)})

    @app.get("/ready")
    async def ready() -> JSONResponse:
        conversation = conversation_status(app)
        if not conversation.get("ready"):
            return error("CONVERSATION_UNAVAILABLE", "对话引擎未就绪。", 503)
        return success({"status": "ready", "control_plane": "ready", "conversation": conversation})

    @app.get("/api/v1/me")
    async def me() -> JSONResponse:
        user_id = _local_user_id(app)
        return success({
            "user": {"id": user_id, "displayName": user_id},
            "workspace": {"id": "local", "name": "Local"},
        })

    @app.get("/api/v1/sessions")
    async def sessions(limit: int = Query(default=50, ge=1, le=100)) -> JSONResponse:
        index = getattr(app.state, "sessions", None)
        if index is None:
            return error("CONVERSATION_UNAVAILABLE", "对话引擎未就绪。", 503)
        rows = await index.list(_local_user_id(app), limit)
        return success({"sessions": [_session_item(row) for row in rows]})

    @app.get("/api/v1/sessions/{thread_id}/conversation")
    async def session_conversation(thread_id: str) -> JSONResponse:
        if getattr(app.state, "sessions", None) is None or getattr(app.state, "agents", None) is None:
            return error("CONVERSATION_UNAVAILABLE", "对话引擎未就绪。", 503)
        from serve.app import load_session_checkpoint

        try:
            row, messages = await load_session_checkpoint(
                app, _local_user_id(app), thread_id, latest=True,
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                return success({
                    "sessionId": thread_id,
                    "title": "",
                    "messages": [],
                    "toolCalls": [],
                    "runEventRefs": [],
                    "agentMessages": [],
                })
            if exc.status_code == 409:
                return error("CONFLICT", str(exc.detail), 409)
            raise
        visible = [message for message in messages if message.get("role") in {"user", "assistant"}]
        return success({
            "sessionId": thread_id,
            "title": row["title"],
            "messages": [_conversation_message(message, index) for index, message in enumerate(visible)],
            "toolCalls": [],
            "runEventRefs": [],
            "agentMessages": messages,
        })

    @app.post("/api/v1/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> Response:
        from serve.app import cancel_active_run

        if not cancel_active_run(app, run_id):
            return error("RESOURCE_NOT_FOUND", "该运行未在执行。", 404)
        return Response(status_code=204)


def register_not_found(app: FastAPI) -> None:
    """Catch-all registered after real routes so unknown paths use the error envelope."""

    @app.api_route("/{path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
    async def not_found(path: str) -> JSONResponse:
        del path
        return error("RESOURCE_NOT_FOUND", "Route not found.", 404)


class ConversationReadyGuard:
    """Return 503 for conversation routes until the in-process engine is ready."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_conversation_path(scope.get("path") or ""):
            fastapi_app = scope.get("app")
            agents = getattr(getattr(fastapi_app, "state", None), "agents", None)
            if agents is None:
                await error("CONVERSATION_UNAVAILABLE", "对话引擎未就绪。", 503)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _local_user_id(app: FastAPI) -> str:
    runtime = getattr(getattr(app.state, "agents", None), "runtime", None) or getattr(
        app.state, "conversation_runtime", None
    )
    return str(getattr(getattr(runtime, "paths", None), "user_id", None) or "default")


def _conversation_message(message: Mapping[str, Any], position: int) -> dict[str, Any]:
    role = message.get("role")
    message_id = str(message.get("id") or f"m{position}")
    return {
        "id": message_id,
        "messageId": message_id,
        "role": role,
        "contentText": _content_text(message.get("content")),
        "position": position,
    }


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, Mapping):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _session_item(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["thread_id"],
        "threadId": row["thread_id"],
        "title": row["title"],
        "titleSource": "fallback",
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "status": row["status"],
        "hasCheckpoint": bool(row["last_checkpoint_id"]),
    }
