from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from datafoundry_api.agent import create_runtime_agent
from datafoundry_api.auth import (
    AuthService,
    attach_auth_cookies,
    clear_auth_cookies,
    identity_from_request,
)
from datafoundry_api.bootstrap import CAPABILITIES, RUN_DEFAULTS, WORKSPACE_CONFIG
from datafoundry_api.envelopes import error, success
from datafoundry_api.errors import AuthError
from datafoundry_api.settings import Settings
from datafoundry_api.store import SqliteStore

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
PUBLIC_PATHS = {
    "/healthz",
    "/ready",
    "/api/v1/auth/status",
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/verify-email",
    "/api/v1/auth/password/forgot",
    "/api/v1/auth/password/reset",
}
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, Idempotency-Key, If-Match, X-CSRF-Token",
    "Access-Control-Max-Age": "86400",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()
    store = SqliteStore(resolved.metadata_db_path)
    auth = AuthService(store, resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved.checkpoint_db_path.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(resolved.checkpoint_db_path)) as checkpointer:
            app.state.langgraph_agent = _build_langgraph_agent(resolved, checkpointer)
            app.state.runtime_ready = True
            yield
        app.state.runtime_ready = False

    app = FastAPI(title="DataFoundry API", version="0.3.0", lifespan=lifespan)
    app.state.settings = resolved
    app.state.store = store
    app.state.auth = auth
    app.state.runtime_ready = False

    @app.middleware("http")
    async def security_and_cors(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method == "OPTIONS" and (
            request.url.path.startswith("/api/v1/") or request.url.path.startswith("/api/copilotkit")
        ):
            return Response(status_code=204, headers=CORS_HEADERS)
        try:
            _enforce_request_security(request, auth)
        except AuthError as exc:
            return _auth_error(exc)
        response = await call_next(request)
        for key, value in CORS_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return success({"status": "ok"})

    @app.get("/ready")
    async def ready() -> JSONResponse:
        return success(
            {
                "status": "ready",
                "control_plane": "ready",
                "runtime": {
                    "provider": "deepagents",
                    "status": "ok" if app.state.runtime_ready else "starting",
                    "streaming": True,
                },
            }
        )

    @app.get("/api/v1/auth/status")
    async def auth_status() -> JSONResponse:
        return success(auth.public_status())

    @app.post("/api/v1/auth/register")
    async def register(request: Request) -> JSONResponse:
        body = await _json_body(request)
        return success(
            auth.register(
                email=_required_string(body, "email"),
                password=_required_string(body, "password"),
                display_name=_optional_string(body, "displayName"),
            ),
            status_code=201,
        )

    @app.post("/api/v1/auth/verify-email")
    async def verify_email(request: Request) -> JSONResponse:
        body = await _json_body(request)
        return success(auth.verify_email(_required_string(body, "token")))

    @app.post("/api/v1/auth/login")
    async def login(request: Request) -> Response:
        body = await _json_body(request)
        payload, session_token, csrf_token, max_age = auth.login(
            email=_required_string(body, "email"),
            password=_required_string(body, "password"),
            client=_optional_string(body, "client"),
        )
        response = success(payload)
        attach_auth_cookies(
            response,
            session_token=session_token,
            csrf_token=csrf_token,
            max_age=max_age,
            settings=resolved,
        )
        return response

    @app.post("/api/v1/auth/csrf/refresh")
    async def csrf_refresh(request: Request) -> Response:
        identity = identity_from_request(request, auth)
        token, max_age = auth.rotate_csrf(identity)
        response = success({"csrfToken": token})
        response.headers["Cache-Control"] = "no-store"
        attach_auth_cookies(
            response,
            session_token=request.cookies.get("df_session") or "",
            csrf_token=token,
            max_age=max_age,
            settings=resolved,
        )
        return response

    @app.post("/api/v1/auth/logout")
    async def logout(request: Request) -> Response:
        identity = identity_from_request(request, auth)
        payload = auth.logout(identity)
        response = success(payload)
        clear_auth_cookies(response, resolved)
        return response

    @app.get("/api/v1/me")
    async def me(request: Request) -> JSONResponse:
        return success(auth.me(identity_from_request(request, auth)))

    @app.get("/api/v1/capabilities")
    async def capabilities() -> JSONResponse:
        return success(CAPABILITIES)

    @app.get("/api/v1/workspace-config")
    async def workspace_config() -> JSONResponse:
        return success(WORKSPACE_CONFIG)

    @app.get("/api/v1/run-defaults")
    async def run_defaults() -> JSONResponse:
        return success(RUN_DEFAULTS)

    @app.get("/api/v1/datasource-types")
    async def datasource_types() -> JSONResponse:
        return success([])

    @app.get("/api/v1/sessions")
    async def sessions() -> JSONResponse:
        return success({"sessions": []})

    @app.get("/api/v1/skills")
    async def skills() -> JSONResponse:
        return success([])

    @app.get("/api/copilotkit/info")
    async def copilotkit_info() -> JSONResponse:
        return JSONResponse(_runtime_info())

    @app.post("/api/copilotkit")
    async def copilotkit(request: Request) -> Response:
        payload = await _json_body(request)
        if payload.get("method") == "info":
            return JSONResponse(_runtime_info())
        if _is_envelope(payload):
            return error("BAD_REQUEST", "Use a standard AG-UI RunAgentInput body.", 400)
        if not resolved.model_configured:
            return error("PROVIDER_CONFIG_MISSING", "LLM provider is not configured.", 503)
        try:
            from ag_ui.core.types import RunAgentInput
            from ag_ui.encoder import EventEncoder
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("ag-ui-langgraph is required") from exc

        try:
            input_data = RunAgentInput.model_validate(
                {
                    "tools": [],
                    "context": [],
                    "forwardedProps": {},
                    **payload,
                }
            )
        except ValueError as exc:
            return error("BAD_REQUEST", str(exc), 400)
        agent = app.state.langgraph_agent.clone()
        encoder = EventEncoder(accept=request.headers.get("accept"))

        async def generate() -> AsyncIterator[str]:
            async for event in agent.run(input_data):
                yield encoder.encode(event)

        return StreamingResponse(
            generate(),
            media_type=encoder.get_content_type(),
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.exception_handler(AuthError)
    async def handle_auth_error(_request: Request, exc: AuthError) -> JSONResponse:
        return _auth_error(exc)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
    async def not_found(path: str) -> JSONResponse:
        return error("RESOURCE_NOT_FOUND", "Route not found.", 404)

    return app


def _build_langgraph_agent(settings: Settings, checkpointer: Any) -> Any:
    from ag_ui_langgraph import LangGraphAgent

    graph = create_runtime_agent(settings, checkpointer=checkpointer)
    return LangGraphAgent(name="dataFoundry", graph=graph)


def _enforce_request_security(request: Request, auth: AuthService) -> None:
    path = request.url.path
    if path in PUBLIC_PATHS or request.method == "OPTIONS":
        return
    if not path.startswith(("/api/v1/", "/api/copilotkit")):
        return
    identity = identity_from_request(request, auth)
    if request.method in UNSAFE_METHODS:
        auth.validate_csrf(identity, request.headers.get("x-csrf-token"))


def _runtime_info() -> dict[str, Any]:
    # Keep agents empty so CopilotKit does not replace the local HttpAgent
    # registered via selfManagedAgents with a single-endpoint proxy.
    return {"version": "1.0.0", "agents": {}}


def _is_envelope(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("method") == "agent/run"


async def _json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuthError(400, "BAD_REQUEST", "Invalid JSON body.") from exc
    if not isinstance(parsed, dict):
        raise AuthError(400, "BAD_REQUEST", "JSON object is required.")
    return parsed


def _required_string(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AuthError(400, "BAD_REQUEST", f"{key} is required.")
    return value.strip()


def _optional_string(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _auth_error(exc: AuthError) -> JSONResponse:
    return error(exc.code, exc.message, exc.status)


