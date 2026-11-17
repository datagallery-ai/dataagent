"""Request-local A2A correlation. No caller identity or conversation state is inferred here."""

import json
import logging
import time
import traceback
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import uuid4

from starlette.responses import JSONResponse

from dataagent.diagnostics import log_event, safe_error

logger = logging.getLogger("dataagent.a2a")


@dataclass
class RequestInfo:
    request_id: str = field(default_factory=lambda: str(uuid4()))
    instance_id: str = ""
    user_id: str = "default"
    rpc_id: str | int | None = None
    method: str | None = None
    message_id: str | None = None
    context_id: str | None = None
    task_id: str | None = None
    root_run_id: str | None = None
    rpc_error_code: int | None = None
    task_state: str | None = None
    stage: str | None = None
    checkpoint_before: str | None = None
    error: dict | None = None

    def fields(self):
        return {
            key: getattr(self, key)
            for key in (
                "request_id",
                "instance_id",
                "user_id",
                "rpc_id",
                "method",
                "message_id",
                "context_id",
                "task_id",
                "root_run_id",
                "stage",
            )
            if getattr(self, key) is not None
        }

    def metadata(self):
        result = {
            "schemaVersion": 1,
            "requestId": self.request_id,
            "messageId": self.message_id,
        }
        if self.root_run_id:
            result["rootRunId"] = self.root_run_id
        if self.error:
            result["error"] = self.error
        return {"dataagent": result}

    def graph_metadata(self):
        return {
            "dataagent_root_run_id": self.root_run_id,
            "dataagent_request_id": self.request_id,
            "dataagent_a2a_task_id": self.task_id,
            "dataagent_a2a_message_id": self.message_id,
            "dataagent_instance_id": self.instance_id,
            "dataagent_rpc_id": self.rpc_id,
        }


current_request: ContextVar[RequestInfo | None] = ContextVar("a2a_request", default=None)


class A2ARejection(ValueError):
    """An adapter validation/conflict error, classified without inspecting its message."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.a2a_code = code


def audit(event, *, request=None, secrets=(), level=logging.INFO, **fields):
    scope = request or current_request.get()
    log_event(
        logger,
        event,
        secrets=secrets,
        level=level,
        **{**(scope.fields() if scope else {}), **fields},
    )


def exception_fields(error, secrets=()):
    """Frame locations and sanitized causes are useful; frame locals and payloads are not logged."""
    return {
        "error_type": type(error).__name__,
        "message": safe_error(error, secrets)["message"],
        "frames": [
            {"file": f.filename, "line": f.lineno, "function": f.name}
            for f in traceback.extract_tb(error.__traceback__)[-12:]
        ],
    }


def route_path(scope):
    path, root = scope.get("path", ""), scope.get("root_path", "")
    if root and path.startswith(root):
        if path == root:
            return ""
        if path[len(root)] == "/":
            return path[len(root) :]
    return path


def instrument_rpc(endpoint):
    """Read only correlation fields; the SDK retains all protocol validation and responses."""

    async def call(request):
        info = current_request.get()
        if info is not None:
            try:
                body = await request.json()
            except (ValueError, UnicodeError):
                body = None
            if isinstance(body, dict):
                rpc_id = body.get("id")
                if type(rpc_id) in (str, int):
                    info.rpc_id = rpc_id
                if isinstance(body.get("method"), str):
                    info.method = body["method"]
                params = body.get("params")
                if isinstance(params, dict):
                    msg = params.get("message")
                    if isinstance(msg, dict):
                        for source, target in (
                            ("messageId", "message_id"),
                            ("contextId", "context_id"),
                            ("taskId", "task_id"),
                        ):
                            if isinstance(msg.get(source), str):
                                setattr(info, target, msg[source])
                    elif isinstance(params.get("id"), str):
                        info.task_id = params["id"]
        response = await endpoint(request)
        if info is not None and isinstance(response, JSONResponse):
            body = json.loads(response.body)
            error = body.get("error")
            if isinstance(error, dict):
                info.rpc_error_code = error.get("code")
            result = body.get("result")
            task = result.get("task", result) if isinstance(result, dict) else None
            if isinstance(task, dict) and isinstance(task.get("status"), dict):
                info.task_id = task.get("id")
                info.context_id = task.get("contextId")
                info.task_state = task["status"].get("state")
                metadata = task.get("metadata")
                details = metadata.get("dataagent") if isinstance(metadata, dict) else None
                root_run_id = details.get("rootRunId") if isinstance(details, dict) else None
                info.root_run_id = root_run_id if isinstance(root_run_id, str) else None
        return response

    return call


class A2AAuditMiddleware:
    """Audit RPC requests, including guard failures, without wrapping Web/TUI SSE."""

    def __init__(self, app, *, instance_id, user_id="default", secrets=()):
        self.app, self.instance_id, self.user_id, self.secrets = app, instance_id, user_id, secrets

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or route_path(scope) != "/a2a/jsonrpc":
            return await self.app(scope, receive, send)
        info = RequestInfo(instance_id=self.instance_id, user_id=self.user_id)
        token = current_request.set(info)
        status = None
        completed = False
        started = time.monotonic()

        async def respond(message):
            nonlocal status, completed
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k != b"x-request-id"]
                message = {
                    **message,
                    "headers": [*headers, (b"x-request-id", info.request_id.encode())],
                }
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True

        try:
            audit("a2a.request.received", request=info, secrets=self.secrets)
            await self.app(scope, receive, respond)
        except Exception as error:
            audit(
                "a2a.request.failed",
                request=info,
                secrets=self.secrets,
                level=logging.ERROR,
                **exception_fields(error, self.secrets),
            )
            if status is not None:
                raise
            await JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "A2A request failed. See X-Request-ID.",
                    },
                },
            )(scope, receive, respond)
        finally:
            audit(
                "a2a.request.finished" if completed else "a2a.request.aborted",
                request=info,
                secrets=self.secrets,
                http_status=status,
                rpc_error_code=info.rpc_error_code,
                task_state=info.task_state,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            current_request.reset(token)
