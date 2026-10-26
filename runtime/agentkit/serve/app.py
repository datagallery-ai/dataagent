"""Local AG-UI endpoint with explicit terminal and recovery semantics."""

import asyncio
import logging
import re
import time
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import uuid4

import anyio
from ag_ui.core import (
    EventType,
    MessagesSnapshotEvent,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
)
from ag_ui.encoder import EventEncoder
from ag_ui_langgraph.utils import langchain_messages_to_agui
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import ConfigDict, model_validator
from starlette.background import BackgroundTask

from dataagent import Runtime, SemanticServiceClient, load_mcp_tools, safe_error
from serve.agents import SessionAgents
from serve.outputs import list_outputs, preview_output
from serve.product import ConversationReadyGuard, register_not_found, register_product_routes
from serve.sessions import Sessions, SessionTurn, public_session, workspace_lock

logger = logging.getLogger("dataagent.api")


class DisconnectAwareResponse(StreamingResponse):
    """Cancel silent model streams too, including ASGI 2.4+ receive disconnects."""

    async def __call__(self, scope, receive, send):
        producer = asyncio.create_task(self.stream_response(send))
        disconnect = asyncio.create_task(self.listen_for_disconnect(receive))
        try:
            done, _ = await asyncio.wait(
                (producer, disconnect), return_when=asyncio.FIRST_COMPLETED
            )
            if producer in done:
                try:
                    await producer
                except OSError:
                    pass  # The connection is already closed; never send a terminal now.
        finally:
            # One edge cancellation lets LangGraph unwind its model/tool tasks. Await
            # it before closing generators, avoiding concurrent anext()/aclose().
            with anyio.CancelScope(shield=True):
                for task in (producer, disconnect):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(producer, disconnect, return_exceptions=True)
                try:
                    await self.body_iterator.aclose()
                finally:
                    if self.background is not None:
                        await self.background()


class QueryInput(RunAgentInput):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, hide_input_in_errors=True)

    @model_validator(mode="after")
    def minimal_query(self):
        if any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value)
            for value in (self.thread_id, self.run_id)
        ):
            raise ValueError("threadId and runId must be safe opaque IDs of at most 128 characters")
        if len(self.messages) != 1 or self.messages[0].role != "user":
            raise ValueError(
                "Send exactly one new user message; the server owns conversation history"
            )
        message = self.messages[0]
        if not isinstance(message.content, str) or not message.content.strip():
            raise ValueError("A nonempty text message is required")
        if getattr(message, "subagent_run_id", None) is not None:
            raise ValueError("Subagent messages are not accepted as user input")
        if self.state not in (None, {}) or self.tools or self.context or self.forwarded_props:
            raise ValueError(
                "Client state, tools, context and forwardedProps injection is unsupported"
            )
        if self.resume or self.parent_run_id:
            raise ValueError("Interrupt resume, edits and run replay are unsupported")
        return self


def agentkit_health_body(runtime: Runtime, instance_id: str) -> dict:
    """Read model and plugin readiness from an in-process runtime. Does not start a process."""
    return {
        "status": "ready",
        "protocol": "dataagent-v2",
        "instanceId": instance_id,
        "model": runtime.model_name,
        "plugins": runtime.settings.plugins.enabled,
        "home": str(runtime.paths.home),
        "entry": runtime.settings.dataagent.entry,
        "user": runtime.paths.user_id,
        "timeoutSeconds": runtime.timeout_seconds,
    }


@asynccontextmanager
async def conversation_lifespan(app, runtime: Runtime, *, model=None):
    """Open the session index, checkpointer and agent cache. Close them when the host exits."""
    with workspace_lock(runtime.paths.state_dir):
        async with AsyncExitStack() as resources:
            semantic_client = (
                SemanticServiceClient.from_settings(runtime.settings.semantic)
                if runtime.settings.semantic is not None
                else None
            )
            if semantic_client is not None:
                resources.push_async_callback(asyncio.to_thread, semantic_client.close)
            mcp_tools = await load_mcp_tools(runtime)
            async with (
                Sessions(runtime.paths.state_dir / "sessions.sqlite") as sessions,
                AsyncSqliteSaver.from_conn_string(
                    str(runtime.paths.state_dir / "checkpoints.sqlite")
                ) as saver,
            ):
                await saver.setup()
                app.state.sessions = sessions
                app.state.checkpointer = saver
                app.state.agents = SessionAgents(
                    runtime,
                    checkpointer=saver,
                    model=model,
                    mcp_tools=mcp_tools,
                    semantic_client=semantic_client,
                )
                try:
                    yield
                finally:
                    app.state.agents.cache.clear()


def _turn_text(messages) -> str:
    selected = None
    for message in messages or ():
        if getattr(message, "role", None) == "user":
            selected = message
    if selected is None and messages:
        selected = messages[-1]
    if selected is None:
        return ""
    content = getattr(selected, "content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return " ".join(parts)


def _release_turn(
    turn: SessionTurn,
    busy: set[tuple[str, str]],
    key: tuple[str, str],
    *,
    unregister=None,
):
    async def release():
        # Also runs if sending response headers fails before the body starts.
        try:
            await turn.release()
        finally:
            busy.discard(key)
            if unregister is not None:
                unregister()

    return release


class _RunCancelled(Exception):
    """The caller asked this run to stop before it reaches a success terminal."""


class _ActiveRun:
    def __init__(self) -> None:
        self.event = asyncio.Event()


_STREAM_END = object()


def _register_run(app, run_id: str) -> _ActiveRun:
    handle = _ActiveRun()
    app.state.active_runs[run_id] = handle
    return handle


def _unregister_run(app, run_id: str, handle: _ActiveRun) -> None:
    runs = getattr(app.state, "active_runs", None)
    if isinstance(runs, dict) and runs.get(run_id) is handle:
        del runs[run_id]


def cancel_active_run(app, run_id: str) -> bool:
    """Stop one live turn. Missing and already finished runs are not success."""
    runs = getattr(app.state, "active_runs", None)
    if not isinstance(runs, dict):
        return False
    handle = runs.get(run_id)
    if handle is None:
        return False
    handle.event.set()
    return True


async def _pull(iterator):
    try:
        return await iterator.__anext__()
    except StopAsyncIteration:
        return _STREAM_END


async def _pull_or_cancel(iterator, handle: _ActiveRun):
    if handle.event.is_set():
        raise _RunCancelled
    pull = asyncio.create_task(_pull(iterator))
    stop = asyncio.create_task(handle.event.wait())
    try:
        await asyncio.wait((pull, stop), return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            pull.cancel()
            stop.cancel()
            await asyncio.gather(pull, stop, return_exceptions=True)
        raise
    if handle.event.is_set():
        if not pull.done():
            pull.cancel()
        await asyncio.gather(pull, return_exceptions=True)
        if not stop.done():
            stop.cancel()
        await asyncio.gather(stop, return_exceptions=True)
        raise _RunCancelled
    if not stop.done():
        stop.cancel()
    await asyncio.gather(stop, return_exceptions=True)
    return pull.result()


async def _iter_until_cancelled(upstream, handle: _ActiveRun):
    iterator = upstream.__aiter__()
    while True:
        event = await _pull_or_cancel(iterator, handle)
        if event is _STREAM_END:
            return
        if handle.event.is_set():
            raise _RunCancelled
        yield event


def _public_run_error(event, secrets: tuple[str, ...]) -> RunErrorEvent:
    """Return a terminal error without credentials or the upstream raw payload."""
    message = safe_error(
        RuntimeError(str(getattr(event, "message", "") or "RUN_ERROR")), secrets,
    )["message"]
    code = safe_error(
        RuntimeError(str(getattr(event, "code", "") or "RUN_ERROR")), secrets,
    )["message"]
    return RunErrorEvent(message=message, code=code)


async def load_session_checkpoint(app, user_id: str, thread_id: str, *, latest: bool = False):
    """Return the session row and AG-UI messages.

    The TUI path keeps the last successful checkpoint. Web refresh passes
    ``latest=True`` so the transcript matches the thread state the model sees,
    including a user message from a run that was stopped.
    """
    row = await app.state.sessions.get(user_id, thread_id)
    if row is None:
        raise HTTPException(404, "Session not found")
    busy = getattr(app.state, "conversation_busy", ())
    if (user_id, thread_id) in busy and not latest:
        raise HTTPException(409, "Session is currently running")
    try:
        opened = await app.state.agents.get(thread_id, create=False)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    if opened is None:
        raise HTTPException(409, "Session binding is missing. Start a new conversation.")
    if (user_id, thread_id) in busy and not latest:
        raise HTTPException(409, "Session is currently running")
    _, agent, _ = opened

    async def read_messages(configurable: dict) -> list:
        state = await agent.graph.aget_state({"configurable": configurable})
        return [
            message.model_dump(by_alias=True)
            for message in langchain_messages_to_agui(state.values.get("messages", []))
        ]

    messages = []
    if latest:
        messages = await read_messages({"thread_id": thread_id})
    elif row["last_checkpoint_id"]:
        messages = await read_messages({
            "thread_id": thread_id,
            "checkpoint_id": row["last_checkpoint_id"],
        })
    return row, messages


async def _acquire_run(app, sessions, busy, user_id: str, thread_id: str, *, rejected_status: int):
    """Reserve one session, then open or create its agent. Release the reservation on failure."""
    if (user_id, thread_id) in busy:
        raise HTTPException(409, "A run is already active for this session")
    busy.add((user_id, thread_id))
    try:
        await _reject_foreign_session(sessions, user_id, thread_id)
        opened = await app.state.agents.get(thread_id, create=True)
    except ValueError as error:
        busy.discard((user_id, thread_id))
        raise HTTPException(rejected_status, str(error)) from None
    except BaseException:
        busy.discard((user_id, thread_id))
        raise
    if opened is None:
        busy.discard((user_id, thread_id))
        raise HTTPException(409, "Session binding is missing. Start a new conversation.")
    return opened


async def _reject_foreign_session(sessions: Sessions, user_id: str, thread_id: str) -> None:
    owner = await sessions.owner(thread_id)
    if owner is not None and owner != user_id:
        raise HTTPException(
            409,
            f"Session {thread_id} is not in profile {user_id}. "
            "Choose an existing session for this profile or start a new one.",
        )


async def _settle(turn: SessionTurn, graph) -> None:
    """Record a checkpoint during cleanup. The turn's own error stays in dataagent.api."""
    try:
        await turn.settle(graph)
    except Exception:
        logger.warning("checkpoint was not recorded for thread=%s", turn.thread_id)


def register_conversation_routes(
    app: FastAPI,
    runtime: Runtime,
    *,
    instance_id: str | None = None,
):
    """Register AG-UI routes on an existing app. The host owns process startup and lifespan."""
    if instance_id is not None:
        app.state.conversation_instance_id = instance_id
    busy: set[tuple[str, str]] = set()
    app.state.conversation_busy = busy
    app.state.active_runs = {}

    def local_user_id() -> str:
        return runtime.paths.user_id

    @app.get("/sessions")
    async def list_sessions(limit: int = Query(default=50, ge=1, le=100)):
        user_id = local_user_id()
        return {
            "sessions": [
                public_session(row) for row in await app.state.sessions.list(user_id, limit)
            ]
        }

    async def outputs_root(thread_id: str):
        user_id = local_user_id()
        try:
            session = runtime.paths.for_session(thread_id)
        except ValueError:
            raise HTTPException(400, "Invalid session id") from None
        if await app.state.sessions.get(user_id, thread_id) is None:
            if await app.state.sessions.owner(thread_id) is not None:
                raise HTTPException(404, "Session not found")
            return None  # A new TUI conversation has no persisted session yet.
        return session.outputs

    @app.get("/sessions/{thread_id}/outputs")
    async def get_outputs(thread_id: str):
        root = await outputs_root(thread_id)
        try:
            return {
                "files": await asyncio.to_thread(list_outputs, root) if root is not None else []
            }
        except OSError:
            raise HTTPException(500, "Unable to list session outputs") from None

    @app.get("/sessions/{thread_id}/outputs/preview")
    async def get_output_preview(thread_id: str, path: str = Query(min_length=1)):
        root = await outputs_root(thread_id)
        if root is None:
            raise HTTPException(404, "Output file not found")
        try:
            return await asyncio.to_thread(preview_output, root, path)
        except OSError:
            raise HTTPException(500, "Unable to read output preview") from None

    @app.get("/sessions/{thread_id}")
    async def get_session(thread_id: str):
        user_id = local_user_id()
        row, messages = await load_session_checkpoint(app, user_id, thread_id)
        interrupted = row["status"] != "complete"
        return {
            **public_session(row),
            "messages": messages,
            "newThreadRequired": not bool(row["last_checkpoint_id"]),
            "notice": (
                "Previous run did not complete. Restored the saved checkpoint."
                if interrupted and messages
                else "No successful checkpoint exists. Start a new conversation."
                if interrupted
                else None
            ),
        }

    @app.post("/dataagent/stream")
    async def stream(body: QueryInput):
        user_id = local_user_id()
        thread_id = body.thread_id
        sessions = app.state.sessions
        opened = await _acquire_run(
            app, sessions, busy, user_id, thread_id, rejected_status=409,
        )
        _session, agent, run_runtime = opened
        graph = agent.graph
        secrets = agent.context.redaction_secrets
        config = {
            "configurable": {"thread_id": thread_id},
            "metadata": {
                "dataagent_root_run_id": body.run_id,
            },
        }
        # AG-UI otherwise supplies recursion_limit=25, overriding Deep Agents' bound
        # limit. Forward only this default: bound callbacks already propagate natively.
        if graph.config and "recursion_limit" in graph.config:
            config["recursion_limit"] = graph.config["recursion_limit"]
        turn = SessionTurn(sessions, user_id, thread_id)
        try:
            row = await sessions.get(user_id, thread_id)
            if row:
                if row["last_run_id"] == body.run_id:
                    raise HTTPException(409, "Run replay is not supported; use a new runId")
                # A missing checkpoint is a failed or not-yet-saved turn. Resume
                # from the latest graph state so a recovered model can accept a
                # new message on this thread. Replay of the same message stays rejected.
                read_config = {"configurable": {"thread_id": thread_id}}
                if row["last_checkpoint_id"]:
                    read_config["configurable"]["checkpoint_id"] = row["last_checkpoint_id"]
                    config["configurable"]["checkpoint_id"] = row["last_checkpoint_id"]
                state = await graph.aget_state(read_config)
                if any(
                    message.id == body.messages[0].id
                    for message in state.values.get("messages", [])
                ):
                    raise HTTPException(409, "Message replay or editing is not supported")
            await turn.start(body.run_id, _turn_text(body.messages))
        except HTTPException:
            busy.discard((user_id, thread_id))
            raise
        except ValueError as error:
            busy.discard((user_id, thread_id))
            raise HTTPException(409, str(error)) from None
        except BaseException:
            busy.discard((user_id, thread_id))
            raise

        handle = _register_run(app, body.run_id)
        release_session = _release_turn(
            turn, busy, (user_id, thread_id),
            unregister=lambda: _unregister_run(app, body.run_id, handle),
        )

        async def events():
            encoder = EventEncoder()
            started = time.monotonic()
            adapter = agent.as_agui(
                name="dataagent-v2",
                emit_raw_events=False,
                subagent_visibility="attributed",
            )
            adapter.config = config
            upstream = None
            try:
                yield encoder.encode(RunStartedEvent(thread_id=thread_id, run_id=body.run_id))
                async with asyncio.timeout(run_runtime.timeout_seconds):
                    upstream = adapter.run(body)
                    terminal = None
                    async for event in _iter_until_cancelled(upstream, handle):
                        if event is None:
                            continue
                        if event.type in (EventType.RUN_FINISHED, EventType.RUN_ERROR):
                            if terminal is not None:
                                raise RuntimeError("Upstream emitted more than one terminal event")
                            terminal = event
                            continue
                        if terminal is not None:
                            raise RuntimeError("Upstream emitted events after its terminal event")
                        # Emit one authoritative final snapshot below. An adapter configured
                        # with an older recovery checkpoint may otherwise snapshot that old state.
                        if event.type in (
                            EventType.RUN_STARTED,
                            EventType.MESSAGES_SNAPSHOT,
                            EventType.STATE_SNAPSHOT,
                        ):
                            continue
                        # Keep AG-UI's native lifecycle/step identities. Child text,
                        # tools and state stay out of the parent conversation stream.
                        if getattr(event, "subagent_run_id", None) and event.type not in (
                            EventType.SUBAGENT_STARTED, EventType.SUBAGENT_FINISHED,
                            EventType.SUBAGENT_ERROR, EventType.STEP_STARTED, EventType.STEP_FINISHED,
                        ):
                            continue
                        if event.type == EventType.SUBAGENT_ERROR:
                            event.message = safe_error(RuntimeError(event.message), secrets)["message"]
                            if event.code:
                                event.code = safe_error(RuntimeError(event.code), secrets)["message"]
                        yield encoder.encode(event)
                    if terminal is None:
                        raise RuntimeError(
                            "INCOMPLETE_STREAM: upstream ended without a terminal event"
                        )
                    if terminal.type == EventType.RUN_ERROR:
                        turn.status = "error"
                        public = _public_run_error(terminal, secrets)
                        logger.error(
                            "RUN_ERROR run=%s thread=%s stage=agent_stream code=%s message=%s",
                            body.run_id,
                            thread_id,
                            public.code,
                            public.message,
                        )
                        yield encoder.encode(public)
                        return
                    latest = await turn.settle(graph, complete=True)
                    yield encoder.encode(
                        MessagesSnapshotEvent(
                            messages=langchain_messages_to_agui(latest.values.get("messages", [])),
                        )
                    )
                    yield encoder.encode(
                        RunFinishedEvent(
                            thread_id=thread_id,
                            run_id=body.run_id,
                            metadata={"durationMs": round((time.monotonic() - started) * 1000)},
                        )
                    )
            except _RunCancelled:
                return
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception as error:
                turn.status = "error"
                details = safe_error(error, secrets)
                logger.error(
                    "RUN_ERROR run=%s thread=%s stage=agent_stream code=%s message=%s",
                    body.run_id,
                    thread_id,
                    details["code"],
                    details["message"],
                )
                yield encoder.encode(RunErrorEvent(**details))
            finally:
                _unregister_run(app, body.run_id, handle)
                with anyio.CancelScope(shield=True):
                    if upstream is not None:
                        await upstream.aclose()
                    if turn.status in {"interrupted", "error"}:
                        await _settle(turn, graph)

        return DisconnectAwareResponse(
            events(),
            background=BackgroundTask(release_session),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/copilotkit/info")
    async def copilotkit_info():
        return JSONResponse(_copilotkit_info())

    @app.post("/api/copilotkit")
    async def copilotkit(request: Request):
        """Run one GUI turn on the same core graph as ``/dataagent/stream``."""
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "Invalid JSON body.") from None
        if not isinstance(payload, dict):
            raise HTTPException(400, "JSON object is required.")
        try:
            body = RunAgentInput.model_validate({
                "tools": [],
                "context": [],
                "forwardedProps": {},
                **payload,
            })
        except ValueError:
            raise HTTPException(400, "Invalid RunAgentInput.") from None

        user_id = local_user_id()
        thread_id = body.thread_id
        sessions = app.state.sessions
        opened = await _acquire_run(
            app, sessions, busy, user_id, thread_id, rejected_status=400,
        )
        _session, agent, run_runtime = opened
        graph = agent.graph
        config = {"configurable": {"thread_id": thread_id}}
        if graph.config and "recursion_limit" in graph.config:
            config["recursion_limit"] = graph.config["recursion_limit"]
        turn = SessionTurn(sessions, user_id, thread_id)
        try:
            await turn.start(body.run_id, _turn_text(body.messages))
        except ValueError as error:
            busy.discard((user_id, thread_id))
            raise HTTPException(409, str(error)) from None
        except BaseException:
            busy.discard((user_id, thread_id))
            raise
        encoder = EventEncoder(accept=request.headers.get("accept"))
        handle = _register_run(app, body.run_id)
        secrets = agent.context.redaction_secrets
        release_gui = _release_turn(
            turn, busy, (user_id, thread_id),
            unregister=lambda: _unregister_run(app, body.run_id, handle),
        )

        async def gui_events():
            adapter = agent.as_agui(
                name="dataagent-v2", config=config, emit_raw_events=False,
            )
            upstream = None
            finished_event = None
            try:
                async with asyncio.timeout(run_runtime.timeout_seconds):
                    upstream = adapter.run(body)
                    async for event in _iter_until_cancelled(upstream, handle):
                        if event is None:
                            continue
                        if event.type == EventType.RUN_FINISHED:
                            finished_event = event
                            continue
                        if event.type == EventType.RUN_ERROR:
                            turn.status = "error"
                            public = _public_run_error(event, secrets)
                            logger.error(
                                "RUN_ERROR run=%s thread=%s stage=copilotkit code=%s message=%s",
                                body.run_id, thread_id, public.code, public.message,
                            )
                            yield encoder.encode(public)
                            return
                        yield encoder.encode(event)
                    if turn.status == "error":
                        return
                    if finished_event is None:
                        raise RuntimeError(
                            "INCOMPLETE_STREAM: upstream ended without a terminal event"
                        )
                    await turn.settle(graph, complete=True)
                    yield encoder.encode(finished_event)
            except _RunCancelled:
                return
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception as error:
                turn.status = "error"
                details = safe_error(error, run_runtime.redaction_secrets)
                logger.error(
                    "RUN_ERROR run=%s thread=%s stage=copilotkit code=%s message=%s",
                    body.run_id, thread_id, details["code"], details["message"],
                )
                yield encoder.encode(RunErrorEvent(**details))
            finally:
                _unregister_run(app, body.run_id, handle)
                with anyio.CancelScope(shield=True):
                    if upstream is not None:
                        await upstream.aclose()
                    if turn.status in {"interrupted", "error"}:
                        await _settle(turn, graph)

        return DisconnectAwareResponse(
            gui_events(),
            background=BackgroundTask(release_gui),
            media_type=encoder.get_content_type(),
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    from a2a_server.server import register_a2a_routes

    register_a2a_routes(app, runtime)


def create_app(runtime: Runtime | None, *, instance_id: str | None = None, model=None):
    """Create the product backend. Web and TUI share this process."""
    identity = instance_id or str(uuid4())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.conversation_runtime = runtime
        app.state.conversation_instance_id = identity
        if runtime is None:
            yield
            return
        started = False
        try:
            async with conversation_lifespan(app, runtime, model=model):
                started = True
                yield
        except Exception as exc:
            if started:
                raise
            logger.warning("Conversation engine failed during startup: %s", exc)
            app.state.agents = None
            app.state.sessions = None
            app.state.checkpointer = None
            yield

    app = FastAPI(title="DataFoundry API", version="0.3.0", lifespan=lifespan)
    app.state.conversation_runtime = runtime
    app.state.conversation_instance_id = identity
    app.add_middleware(ConversationReadyGuard)
    register_product_routes(app)
    if runtime is not None:
        register_conversation_routes(app, runtime, instance_id=identity)
    register_not_found(app)
    return app


def _copilotkit_info():
    # Empty agents keeps a client-registered HttpAgent from being replaced.
    return {"version": "1.0.0", "agents": {}}
