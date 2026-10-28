"""Assemble and run an A2A 1.0 server in front of an AgentKit runtime."""

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.server.routes import create_jsonrpc_routes
from a2a.server.tasks import TaskStore
from a2a.types.a2a_pb2 import AgentCard
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from langchain_core.messages import HumanMessage
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_server.agent_card import JSONRPC_PATH, build_agent_card
from a2a_server.agent_executor import AgentKitExecutor
from a2a_server.task_store import create_task_store
from dataagent.bootstrap import Runtime
from serve.sessions import SessionTurn

_FOREIGN_SESSION = (
    "Session {thread_id} is not in profile {user_id}. "
    "Choose an existing session for this profile or start a new one."
)


def _reserve(busy, user_id: str, thread_id: str):
    """Claim the shared session before any await. Missing lock means this host has no GUI."""
    if not isinstance(busy, set):
        return None, None
    key = (user_id, thread_id)
    if key in busy:
        raise ValueError("A run is already active for this session")
    busy.add(key)
    return busy, key


async def _stop(turn, graph, *, error: bool = False) -> None:
    """Record a cancelled or failed turn, then release it."""
    if turn is None:
        return
    if error:
        turn.status = "error"
    if graph is not None:
        from serve.app import _settle

        await _settle(turn, graph)
    await turn.release()


class AgentHost:
    """Bridge used by routes that share the host process's agent cache."""

    def __init__(self, runtime: Runtime, *, app=None):
        self.runtime = runtime
        self.app = app

    async def reply(self, thread_id: str, text: str):
        """Run one turn on the shared session index and checkpoint."""
        if self.app is None or getattr(self.app.state, "agents", None) is None:
            raise RuntimeError("A2A server is not ready")
        user_id = self.runtime.paths.user_id
        busy, key = _reserve(getattr(self.app.state, "conversation_busy", None), user_id, thread_id)
        turn = graph = None
        try:
            turn, config = await self._prepare(thread_id, text, user_id)
            opened = await self.app.state.agents.get(thread_id, create=True)
            if opened is None:
                raise RuntimeError("Session could not be opened")
            _session, handle, _runtime = opened
            graph = handle.graph
            if graph.config and "recursion_limit" in graph.config:
                config["recursion_limit"] = graph.config["recursion_limit"]
            async with asyncio.timeout(self.runtime.timeout_seconds):
                result = await handle.ainvoke({"messages": [HumanMessage(content=text)]}, config)
            if turn is not None:
                await turn.settle(graph, complete=True)
            return result
        except asyncio.CancelledError:
            await _stop(turn, graph)
            raise
        except Exception:
            await _stop(turn, graph, error=True)
            raise
        finally:
            if busy is not None:
                busy.discard(key)

    async def _prepare(self, thread_id: str, text: str, user_id: str):
        run_id = str(uuid4())
        config = {
            "configurable": {"thread_id": thread_id},
            "metadata": {"dataagent_root_run_id": run_id},
        }
        sessions = getattr(self.app.state, "sessions", None)
        if sessions is None:
            return None, config
        owner = await sessions.owner(thread_id)
        if owner is not None and owner != user_id:
            raise ValueError(_FOREIGN_SESSION.format(thread_id=thread_id, user_id=user_id))
        row = await sessions.get(user_id, thread_id)
        if row and row["last_checkpoint_id"]:
            config["configurable"]["checkpoint_id"] = row["last_checkpoint_id"]
        turn = SessionTurn(sessions, user_id, thread_id)
        await turn.start(run_id, text)
        return turn, config


def _bearer_middleware(token: str, exclude_paths: tuple[str, ...]):
    from starlette.middleware.base import BaseHTTPMiddleware

    class BearerAuthMiddleware(BaseHTTPMiddleware):
        """Require ``Authorization: Bearer <token>`` except on discovery routes."""

        async def dispatch(self, request: Request, call_next):
            if request.url.path in exclude_paths:
                return await call_next(request)
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] != token:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized: invalid or missing Bearer token"},
                )
            return await call_next(request)

    return Middleware(BearerAuthMiddleware)


def _agent_card_routes(agent_card: AgentCard) -> list:
    """Serve the card with the JSON-RPC URL taken from the request host."""

    async def get_agent_card(request: Request) -> JSONResponse:
        card = agent_card_to_dict(agent_card)
        host = request.headers.get("host", "127.0.0.1")
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme or "http")
        base_url = f"{scheme}://{host}"
        for interface in card.get("supportedInterfaces", []):
            if interface.get("protocolBinding") == "JSONRPC":
                interface["url"] = f"{base_url}{JSONRPC_PATH}"
        return JSONResponse(card)

    return [Route(AGENT_CARD_WELL_KNOWN_PATH, endpoint=get_agent_card, methods=["GET"])]


def build_a2a_routes(
    runtime: Runtime,
    *,
    app,
    host: str | None = None,
    port: int | None = None,
    executor: AgentExecutor | None = None,
    task_store: TaskStore | None = None,
) -> list:
    """Routes for JSON-RPC and the Agent Card. The host app owns the lifespan."""
    agent_host = AgentHost(runtime, app=app)
    card = build_agent_card(
        host=host or runtime.settings.server.host,
        port=runtime.settings.server.port if port is None else port,
    )
    if executor is None:
        executor = AgentKitExecutor(agent_host.reply, secrets=tuple(runtime.redaction_secrets))
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store or create_task_store(),
        agent_card=card,
    )
    return [
        *_agent_card_routes(card),
        *create_jsonrpc_routes(handler, rpc_url=JSONRPC_PATH),
    ]


def register_a2a_routes(
    app,
    runtime: Runtime,
    *,
    host: str | None = None,
    port: int | None = None,
    executor: AgentExecutor | None = None,
    task_store: TaskStore | None = None,
) -> None:
    """Attach A2A routes to a process that already opened sessions and the checkpointer."""
    app.router.routes.extend(build_a2a_routes(
        runtime, app=app, host=host, port=port, executor=executor, task_store=task_store,
    ))


def create_a2a_server(
    runtime: Runtime,
    *,
    host: str,
    port: int,
    auth_token: str | None = None,
    auth_exclude_paths: tuple[str, ...] = (AGENT_CARD_WELL_KNOWN_PATH,),
    model=None,
    task_store: TaskStore | None = None,
    executor: AgentExecutor | None = None,
) -> Starlette:
    """Build a standalone Starlette app. Tests and ``serve-a2a`` use this.

    ``dataagent serve`` and the control plane mount the same routes on the process
    that already holds the workspace lock. This app takes that lock too, so it
    cannot share a Home with those hosts.
    """
    if not host:
        raise ValueError("Host must be specified")

    @asynccontextmanager
    async def lifespan(app: Starlette):
        from serve.app import conversation_lifespan

        async with conversation_lifespan(app, runtime, model=model):
            yield

    middleware = []
    if auth_token:
        middleware.append(_bearer_middleware(auth_token, auth_exclude_paths))
    app = Starlette(lifespan=lifespan, middleware=middleware)
    register_a2a_routes(
        app, runtime, host=host, port=port, executor=executor, task_store=task_store,
    )
    return app


def run_a2a_server(
    runtime: Runtime,
    *,
    host: str,
    port: int,
    auth_token: str | None = None,
) -> None:
    """Serve until the process is interrupted. The caller checks the port first."""
    import uvicorn

    app = create_a2a_server(runtime, host=host, port=port, auth_token=auth_token)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=5,
    )
