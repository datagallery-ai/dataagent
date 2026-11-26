"""Assemble and run an A2A 1.0 server in front of an AgentKit runtime."""

import asyncio
import hmac
import logging
from collections.abc import Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from dataclasses import replace
from uuid import uuid4

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.server.routes import create_jsonrpc_routes
from a2a.server.tasks import TaskStore
from a2a.types.a2a_pb2 import AgentCard, GetTaskRequest, Task, TaskState, TaskStatusUpdateEvent
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from a2a.utils.task import apply_history_length
from langchain.agents.middleware.internal_call_transformer import INTERNAL_CALL_METADATA_KEY
from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.types import Command
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from dataagent import PreparedRuntime
from dataagent.host import ServerSettings
from dataagent.server.a2a.agent_card import JSONRPC_PATH, build_agent_card
from dataagent.server.a2a.agent_executor import AgentKitExecutor, HITLContextBuilder, message_text
from dataagent.server.a2a.request_context import (
    A2AAuditMiddleware,
    A2ARejection,
    audit,
    current_request,
    exception_fields,
    instrument_rpc,
    route_path,
)
from dataagent.server.a2a.task_store import create_task_store
from dataagent.server.sessions import SessionTurn, continuation_configurable

_FOREIGN_SESSION = (
    "Session {thread_id} does not belong to user {user_id}. "
    "Choose one of this user's sessions or start a new one."
)


def _reserve(busy, user_id: str, thread_id: str):
    """Claim the shared session before any await. Missing lock means this host has no GUI."""
    if not isinstance(busy, set):
        return None, None
    key = (user_id, thread_id)
    if key in busy:
        raise A2ARejection("A run is already active for this session", "CONFLICT")
    busy.add(key)
    return busy, key


async def _stop(turn, graph, *, error: bool = False, secrets=()) -> None:
    """Record a cancelled or failed turn, then release it."""
    if turn is None:
        return
    turn.status = "error" if error else "interrupted"
    try:
        if graph is not None:
            latest = await turn.settle(graph)
            checkpoint_id = (latest.config or {}).get("configurable", {}).get("checkpoint_id")
            audit(
                "a2a.checkpoint.saved" if checkpoint_id else "a2a.checkpoint.unchanged",
                secrets=secrets,
                checkpoint_id=checkpoint_id,
                complete=False,
            )
    except Exception as cleanup_error:
        audit(
            "a2a.checkpoint.failed",
            secrets=secrets,
            level=logging.ERROR,
            **exception_fields(cleanup_error, secrets),
        )
    finally:
        try:
            await turn.release()
        except Exception as cleanup_error:
            audit(
                "a2a.cleanup.failed",
                secrets=secrets,
                level=logging.ERROR,
                **exception_fields(cleanup_error, secrets),
            )


class _RequestHandler(DefaultRequestHandler):
    async def on_message_send_stream(self, params, context):
        # The SDK emits the Task on creation, but only updates on continuation.
        async with aclosing(super().on_message_send_stream(params, context)) as events:
            event = await anext(events, None)
            initial = context.state.pop("dataagent_initial_task", None)
            if event is None:
                # A validated retry can become stale while it waits in the SDK queue.
                if initial is None:
                    return
                current = await self.on_get_task(GetTaskRequest(id=initial.id), context)
                event = apply_history_length(current, params.configuration)
            elif initial is not None and not isinstance(event, Task):
                yield apply_history_length(initial, params.configuration)

            info = current_request.get()
            while event is not None:
                if info is not None and isinstance(event, (Task, TaskStatusUpdateEvent)):
                    info.task_id = event.id if isinstance(event, Task) else event.task_id
                    info.context_id = event.context_id
                    info.task_state = TaskState.Name(event.status.state)
                yield event
                event = await anext(events, None)


class AgentHost:
    """Bridge used by routes that share the host process's agent cache."""

    def __init__(self, runtime: PreparedRuntime, *, app=None):
        self.runtime = runtime
        self.app = app

    async def reply(
        self,
        thread_id: str,
        inputs: str | Command,
        on_text: Callable[[str], Awaitable[None]] | None = None,
    ):
        """Run one turn on the shared session index and checkpoint."""
        invocation = current_request.get()
        if invocation:
            invocation.stage = "session_prepare"
        if self.app is None or getattr(self.app.state, "agents", None) is None:
            raise RuntimeError("A2A server is not ready")
        user_id = self.runtime.paths.user_id
        busy, key = _reserve(getattr(self.app.state, "conversation_busy", None), user_id, thread_id)
        turn = graph = None
        try:
            prepared_turn, config = await self._prepare(thread_id, user_id)
            opened = await self.app.state.agents.get(thread_id, create=True)
            if opened is None:
                raise RuntimeError("Session could not be opened")
            _session, handle, _runtime = opened
            graph = handle.graph
            if isinstance(inputs, Command):
                thread_config = {"configurable": {"thread_id": thread_id}}
                snapshot = await graph.aget_state(thread_config)
                pending = {item.id for item in snapshot.interrupts}
                checkpoint_id = config["configurable"].get("checkpoint_id")
                if (
                    not checkpoint_id
                    or checkpoint_id != snapshot.config["configurable"].get("checkpoint_id")
                    or not pending
                    or not set(inputs.resume).issubset(pending)
                ):
                    raise A2ARejection("The interrupt is no longer pending", "BAD_REQUEST")
                # Addressing a checkpoint explicitly starts a replay and ignores successful
                # sibling writes from the interrupted step. Continue from the thread head.
                config["configurable"] = thread_config["configurable"]
                graph_input = inputs
            else:
                snapshot = await graph.aget_state(config)
                if snapshot.interrupts:
                    raise A2ARejection("This session is waiting for a HITL response", "CONFLICT")
                graph_input = {"messages": [HumanMessage(content=inputs)]}
            turn = prepared_turn
            if turn is not None:
                await turn.start(
                    config["metadata"]["dataagent_root_run_id"],
                    inputs if isinstance(inputs, str) else "HITL response",
                )
            if graph.config and "recursion_limit" in graph.config:
                config["recursion_limit"] = graph.config["recursion_limit"]
            if invocation:
                invocation.stage = "agent_invoke"
            async with asyncio.timeout(self.runtime.timeout_seconds):
                if on_text is None:
                    result = await handle.ainvoke(graph_input, config)
                else:
                    result = {}
                    interrupts = {}
                    entry = self.runtime.settings.dataagent.entry
                    async with aclosing(
                        handle.astream(
                            graph_input,
                            config,
                            stream_mode=["messages", "values"],
                            version="v2",
                            subgraphs=bool(entry),
                        )
                    ) as stream:
                        async for event in stream:
                            ns = event["ns"]
                            if event["type"] == "values":
                                if not ns:
                                    result = event["data"]
                                    interrupts.update(
                                        (item.id, item) for item in event["interrupts"]
                                    )
                                continue
                            message, metadata = event["data"]
                            if metadata.get(INTERNAL_CALL_METADATA_KEY):
                                continue
                            node = metadata.get("langgraph_node")
                            if entry:
                                in_entry = (not ns and node == entry) or (
                                    len(ns) == 1 and ns[0].split(":", 1)[0] == entry
                                )
                                visible = (
                                    in_entry
                                    and node != "tools"
                                    and "dataagent:a2a-public-text" in metadata.get("tags", ())
                                )
                            else:
                                visible = not ns and node == "model"
                            if visible and isinstance(message, AIMessageChunk):
                                text = message_text(message)
                                if text:
                                    await on_text(text)
                    if interrupts:
                        result = {**result, "__interrupt__": tuple(interrupts.values())}
            if turn is not None:
                if invocation:
                    invocation.stage = "checkpoint_settle"
                waiting = bool(result.get("__interrupt__"))
                if waiting:
                    turn.status = "waiting"
                latest = await turn.settle(graph, complete=not waiting)
                audit(
                    "a2a.checkpoint.saved",
                    secrets=self.runtime.redaction_secrets,
                    checkpoint_id_before=invocation.checkpoint_before if invocation else None,
                    checkpoint_id=latest.config["configurable"].get("checkpoint_id"),
                    complete=not waiting,
                )
            return result
        except asyncio.CancelledError:
            await _stop(turn, graph, secrets=self.runtime.redaction_secrets)
            raise
        except Exception:
            await _stop(turn, graph, error=True, secrets=self.runtime.redaction_secrets)
            raise
        finally:
            if busy is not None:
                busy.discard(key)

    async def cancel_waiting(self, thread_id: str, task_id: str) -> None:
        """Close the waiting session entry and leave its checkpoint intact."""
        sessions = self.app.state.sessions
        user_id = self.runtime.paths.user_id
        row = await sessions.get(user_id, thread_id)
        if row is None or row["status"] != "waiting" or not row["last_checkpoint_id"]:
            return
        checkpoint = await self.app.state.checkpointer.aget_tuple(
            {
                "configurable": continuation_configurable(thread_id, row),
            }
        )
        if checkpoint is not None and checkpoint.metadata.get("dataagent_a2a_task_id") == task_id:
            await sessions.interrupt_waiting(user_id, thread_id, row["last_run_id"])

    async def _prepare(self, thread_id: str, user_id: str):
        invocation = current_request.get()
        run_id = invocation.root_run_id if invocation and invocation.root_run_id else str(uuid4())
        if invocation:
            invocation.root_run_id = run_id
        config = {
            "configurable": {"thread_id": thread_id},
            "metadata": invocation.graph_metadata()
            if invocation
            else {
                "dataagent_root_run_id": run_id,
            },
        }
        sessions = getattr(self.app.state, "sessions", None)
        if sessions is None:
            return None, config
        owner = await sessions.owner(thread_id)
        if owner is not None and owner != user_id:
            raise A2ARejection(
                _FOREIGN_SESSION.format(thread_id=thread_id, user_id=user_id),
                "FORBIDDEN",
            )
        row = await sessions.get(user_id, thread_id)
        if invocation:
            invocation.checkpoint_before = row.get("last_checkpoint_id") if row else None
        config["configurable"] = continuation_configurable(thread_id, row)
        turn = SessionTurn(sessions, user_id, thread_id)
        return turn, config


def _route_path(scope) -> str:
    """Path inside this app, as Starlette routing sees it.

    Strip ``root_path`` only on a path-segment boundary: with ``root_path="/a"``,
    ``/a2a/jsonrpc`` stays as is and ``/a/a2a/jsonrpc`` becomes ``/a2a/jsonrpc``.
    """
    return route_path(scope)


class BearerGuard:
    """Pure ASGI guard: require ``Authorization: Bearer <token>`` on the protected paths.

    Other paths pass through untouched, so SSE responses are never wrapped.
    """

    def __init__(self, app, *, token: str, paths: frozenset[str]):
        self.app = app
        self.expected = f"Bearer {token}".encode()
        self.paths = paths

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and _route_path(scope) in self.paths:
            supplied = dict(scope.get("headers") or ()).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                audit("a2a.request.rejected", error_code="UNAUTHORIZED", stage="authentication")
                response = JSONResponse(
                    status_code=401,
                    content={
                        "success": False,
                        "error": {
                            "code": "UNAUTHORIZED",
                            "message": "Invalid or missing Bearer token.",
                        },
                    },
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


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
    runtime: PreparedRuntime | None,
    *,
    app,
    host: str | None = None,
    port: int | None = None,
    executor: AgentExecutor | None = None,
    task_store: TaskStore | None = None,
) -> list:
    """Serve discovery even without a runtime; JSON-RPC needs the host's live engine."""
    server = runtime.settings.server if runtime is not None else ServerSettings()
    card = build_agent_card(
        host=host or server.host,
        port=server.port if port is None else port,
        requires_bearer=runtime is not None and bool(runtime.a2a_auth_token),
        streaming=runtime is not None and executor is None,
    )
    routes = _agent_card_routes(card)
    if runtime is None:
        return routes
    if executor is None:
        agent_host = AgentHost(runtime, app=app)
        executor = AgentKitExecutor(
            agent_host.reply,
            secrets=tuple(runtime.redaction_secrets),
            cancel_waiting=agent_host.cancel_waiting,
            stream_reply=agent_host.reply,
        )
    store = task_store or create_task_store()
    handler = _RequestHandler(
        agent_executor=executor,
        task_store=store,
        agent_card=card,
        request_context_builder=(
            HITLContextBuilder(store, runtime.redaction_secrets)
            if type(executor).execute is AgentKitExecutor.execute
            else None
        ),
    )
    rpc_routes = create_jsonrpc_routes(handler, rpc_url=JSONRPC_PATH)
    return [
        *routes,
        *(
            Route(route.path, endpoint=instrument_rpc(route.endpoint), methods=["POST"])
            for route in rpc_routes
        ),
    ]


def register_a2a_routes(
    app,
    runtime: PreparedRuntime | None,
    *,
    host: str | None = None,
    port: int | None = None,
    executor: AgentExecutor | None = None,
    task_store: TaskStore | None = None,
) -> None:
    """Attach discovery and, when a runtime is available, the shared engine's RPC routes."""
    app.router.routes.extend(
        build_a2a_routes(
            runtime,
            app=app,
            host=host,
            port=port,
            executor=executor,
            task_store=task_store,
        )
    )


def create_a2a_server(
    runtime: PreparedRuntime,
    *,
    host: str,
    port: int,
    auth_token: str | None = None,
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
    # The explicit CLI token must be redacted by graph traces and operator logs too.
    if auth_token:
        runtime = replace(runtime, a2a_auth_token=auth_token)
    instance_id = str(uuid4())

    @asynccontextmanager
    async def lifespan(app: Starlette):
        from dataagent.server.app import conversation_lifespan

        async with conversation_lifespan(app, runtime, model=model):
            yield

    middleware = [
        Middleware(
            A2AAuditMiddleware,
            instance_id=instance_id,
            user_id=runtime.paths.user_id,
            secrets=runtime.redaction_secrets,
        )
    ]
    if runtime.a2a_auth_token:
        middleware.append(
            Middleware(BearerGuard, token=runtime.a2a_auth_token, paths=frozenset({JSONRPC_PATH}))
        )
    app = Starlette(lifespan=lifespan, middleware=middleware)
    app.state.conversation_instance_id = instance_id
    register_a2a_routes(
        app,
        runtime,
        host=host,
        port=port,
        executor=executor,
        task_store=task_store,
    )
    return app


def run_a2a_server(
    runtime: PreparedRuntime,
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
