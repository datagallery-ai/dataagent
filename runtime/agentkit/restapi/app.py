"""Local AG-UI endpoint with explicit terminal and recovery semantics."""

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
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
from ag_ui_langgraph import LangGraphAgent
from ag_ui_langgraph.utils import langchain_messages_to_agui
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import ConfigDict, model_validator
from starlette.background import BackgroundTask

from dataagent import Runtime, load_mcp_tools, safe_error
from restapi.agents import SessionAgents
from restapi.outputs import list_outputs, preview_output
from restapi.sessions import Sessions, public_session, workspace_lock

logger = logging.getLogger("dataagent.api")


class DisconnectAwareResponse(StreamingResponse):
    """Cancel silent model streams too, including ASGI 2.4+ receive disconnects."""

    async def __call__(self, scope, receive, send):
        producer = asyncio.create_task(self.stream_response(send))
        disconnect = asyncio.create_task(self.listen_for_disconnect(receive))
        try:
            done, _ = await asyncio.wait((producer, disconnect), return_when=asyncio.FIRST_COMPLETED)
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
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value)
               for value in (self.thread_id, self.run_id)):
            raise ValueError("threadId and runId must be safe opaque IDs of at most 128 characters")
        if len(self.messages) != 1 or self.messages[0].role != "user":
            raise ValueError("Send exactly one new user message; the server owns conversation history")
        message = self.messages[0]
        if not isinstance(message.content, str) or not message.content.strip():
            raise ValueError("A nonempty text message is required")
        if getattr(message, "subagent_run_id", None) is not None:
            raise ValueError("Subagent messages are not accepted as user input")
        if self.state not in (None, {}) or self.tools or self.context or self.forwarded_props:
            raise ValueError("Client state, tools, context and forwardedProps injection is unsupported")
        if self.resume or self.parent_run_id:
            raise ValueError("Interrupt resume, edits and run replay are unsupported")
        return self


def create_app(runtime: Runtime, *, instance_id: str | None = None, model=None):
    identity = instance_id or str(uuid4())
    user_id = runtime.paths.user_id
    busy: set[tuple[str, str]] = set()

    @asynccontextmanager
    async def lifespan(app):
        with workspace_lock(runtime.paths.state_dir):
            mcp_tools = await load_mcp_tools(runtime)
            async with (
                Sessions(runtime.paths.state_dir / "sessions.sqlite") as sessions,
                AsyncSqliteSaver.from_conn_string(str(runtime.paths.state_dir / "checkpoints.sqlite")) as saver,
            ):
                await saver.setup()
                app.state.sessions = sessions
                app.state.checkpointer = saver
                app.state.agents = SessionAgents(
                    runtime, checkpointer=saver, model=model, mcp_tools=mcp_tools,
                )
                try:
                    yield
                finally:
                    app.state.agents.graphs.clear()
    app = FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    async def health():
        runtime = app.state.agents.runtime
        return {
            "status": "ready", "protocol": "dataagent-v2", "instanceId": identity,
            "model": runtime.model_name,
            "plugins": runtime.settings.plugins.enabled,
            "user": user_id,
            "home": str(runtime.paths.home),
            "workspaces": [
                {"name": item.name, "path": str(item.path)} for item in runtime.paths.workspaces
            ],
            "stateDir": str(runtime.paths.state_dir),
            "timeoutSeconds": runtime.timeout_seconds,
        }

    @app.get("/sessions")
    async def list_sessions(limit: int = Query(default=50, ge=1, le=100)):
        return {"sessions": [public_session(row) for row in await app.state.sessions.list(user_id, limit)]}

    async def outputs_root(thread_id: str):
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
            return {"files": await asyncio.to_thread(list_outputs, root) if root is not None else []}
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
        row = await app.state.sessions.get(user_id, thread_id)
        if row is None:
            raise HTTPException(404, "Session not found")
        if (user_id, thread_id) in busy:
            raise HTTPException(409, "Session is currently running")
        try:
            opened = await app.state.agents.get(thread_id, create=False)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        if opened is None:
            raise HTTPException(409, "Session binding is missing. Start a new conversation.")
        if (user_id, thread_id) in busy:
            raise HTTPException(409, "Session is currently running")
        _, graph, _ = opened
        messages = []
        if row["last_checkpoint_id"]:
            state = await graph.aget_state({"configurable": {
                "thread_id": thread_id, "checkpoint_id": row["last_checkpoint_id"],
            }})
            messages = [message.model_dump(by_alias=True) for message in
                        langchain_messages_to_agui(state.values.get("messages", []))]
        interrupted = row["status"] != "complete"
        return {
            **public_session(row), "messages": messages,
            "newThreadRequired": not bool(row["last_checkpoint_id"]),
            "notice": (
                "Previous run did not complete. Restored the last successful checkpoint; no work was replayed."
                if interrupted and messages else
                "No successful checkpoint exists. Start a new conversation." if interrupted else None
            ),
        }

    @app.post("/dataagent/stream")
    async def stream(body: QueryInput, request: Request):
        thread_id = body.thread_id
        if (user_id, thread_id) in busy:
            raise HTTPException(409, "A run is already active for this session")
        busy.add((user_id, thread_id))
        sessions = app.state.sessions
        try:
            owner = await sessions.owner(thread_id)
            if owner is not None and owner != user_id:
                raise HTTPException(
                    409,
                    f"Session {thread_id} is not in profile {user_id}. "
                    "Choose an existing session for this profile or start a new one.",
                )
            opened = await app.state.agents.get(thread_id, create=True)
        except ValueError as error:
            busy.discard((user_id, thread_id))
            raise HTTPException(409, str(error)) from None
        except BaseException:
            busy.discard((user_id, thread_id))
            raise
        session, graph, run_runtime = opened
        secrets = run_runtime.redaction_secrets
        config = {"configurable": {"thread_id": thread_id}, "metadata": {
            "dataagent_root_run_id": body.run_id,
        }}
        # AG-UI otherwise supplies recursion_limit=25, overriding Deep Agents' bound
        # limit. Forward only this default: bound callbacks already propagate natively.
        if graph.config and "recursion_limit" in graph.config:
            config["recursion_limit"] = graph.config["recursion_limit"]
        try:
            row = await sessions.get(user_id, thread_id)
            if row:
                if row["last_run_id"] == body.run_id:
                    raise HTTPException(409, "Run replay is not supported; use a new runId")
                if not row["last_checkpoint_id"]:
                    raise HTTPException(409, "No successful checkpoint exists; start a new conversation")
                config["configurable"]["checkpoint_id"] = row["last_checkpoint_id"]
                state = await graph.aget_state(config)
                if any(message.id == body.messages[0].id for message in state.values.get("messages", [])):
                    raise HTTPException(409, "Message replay or editing is not supported")
            await sessions.start(user_id, thread_id, body.run_id, body.messages[0].content)
        except HTTPException:
            busy.discard((user_id, thread_id))
            raise
        except ValueError as error:
            busy.discard((user_id, thread_id))
            raise HTTPException(409, str(error)) from None
        except BaseException:
            busy.discard((user_id, thread_id))
            raise

        status = "interrupted"

        async def release_session():
            # Also runs if sending response headers fails before events() starts.
            try:
                if status != "complete":
                    await sessions.finish(user_id, thread_id, status)
            finally:
                busy.discard((user_id, thread_id))

        async def events():
            nonlocal status
            encoder = EventEncoder()
            started = time.monotonic()
            adapter = LangGraphAgent(
                name="dataagent-v2", graph=graph, emit_raw_events=False, subagent_visibility="hidden",
            )
            adapter.config = config
            upstream = None
            try:
                yield encoder.encode(RunStartedEvent(thread_id=thread_id, run_id=body.run_id))
                async with asyncio.timeout(run_runtime.timeout_seconds):
                    upstream = adapter.run(body)
                    terminal = None
                    async for event in upstream:
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
                            EventType.RUN_STARTED, EventType.MESSAGES_SNAPSHOT, EventType.STATE_SNAPSHOT,
                        ):
                            continue
                        yield encoder.encode(event)
                    if terminal is None:
                        raise RuntimeError("INCOMPLETE_STREAM: upstream ended without a terminal event")
                    if terminal.type == EventType.RUN_ERROR:
                        status = "error"
                        await sessions.finish(user_id, thread_id, status)
                        message = safe_error(RuntimeError(terminal.message), secrets)["message"]
                        code = safe_error(RuntimeError(terminal.code or "RUN_ERROR"), secrets)["message"]
                        logger.error(
                            "RUN_ERROR run=%s thread=%s stage=agent_stream code=%s message=%s",
                            body.run_id, thread_id, code, message,
                        )
                        yield encoder.encode(RunErrorEvent(message=message, code=code))
                        return
                    latest = await graph.aget_state({"configurable": {"thread_id": thread_id}})
                    if latest.next:
                        raise RuntimeError("Agent stopped with unfinished work or an unsupported interrupt")
                    checkpoint_id = latest.config["configurable"]["checkpoint_id"]
                    await sessions.finish(user_id, thread_id, "complete", checkpoint_id)
                    status = "complete"
                    yield encoder.encode(MessagesSnapshotEvent(
                        messages=langchain_messages_to_agui(latest.values.get("messages", [])),
                    ))
                    yield encoder.encode(RunFinishedEvent(
                        thread_id=thread_id, run_id=body.run_id,
                        metadata={"durationMs": round((time.monotonic() - started) * 1000)},
                    ))
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception as error:
                status = "error"
                details = safe_error(error, secrets)
                logger.error(
                    "RUN_ERROR run=%s thread=%s stage=agent_stream code=%s message=%s",
                    body.run_id, thread_id, details["code"], details["message"],
                )
                yield encoder.encode(RunErrorEvent(**details))
            finally:
                with anyio.CancelScope(shield=True):
                    if upstream is not None:
                        await upstream.aclose()

        return DisconnectAwareResponse(events(), background=BackgroundTask(release_session), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    return app
