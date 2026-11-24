"""Bridge one A2A 1.0 task to a single AgentKit graph invocation."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

from a2a.helpers import (
    new_data_part,
    new_task,
    new_text_artifact_update_event,
    new_text_status_update_event,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext, SimpleRequestContextBuilder
from a2a.server.events import EventQueue
from a2a.types.a2a_pb2 import TaskState
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict
from langgraph.types import Command

from dataagent import safe_error
from dataagent.host import log_context, opaque_id, safe_interrupt_fields
from dataagent.server.a2a.request_context import (
    A2ARejection,
    RequestInfo,
    audit,
    current_request,
    exception_fields,
)

Reply = Callable[[str, str | Command], Awaitable[object]]


@asynccontextmanager
async def _artifact_preview(event_queue, task_id, context_id, artifact_id):
    """Send the first text immediately and combine subsequent bursts into timed updates."""
    pending: list[str] = []
    flush_now = asyncio.Event()
    worker: asyncio.Task | None = None
    started = False

    async def send(text, *, append):
        await event_queue.enqueue_event(
            new_text_artifact_update_event(
                task_id,
                context_id,
                "dataagent_result",
                text,
                artifact_id=artifact_id,
                append=append,
            )
        )

    async def flush_batches():
        while pending:
            try:
                await asyncio.wait_for(flush_now.wait(), timeout=0.1)
            except TimeoutError:
                pass
            flush_now.clear()
            text = "".join(pending)
            pending.clear()
            await send(text, append=True)

    async def on_text(text):
        nonlocal started, worker
        if not started:
            await send(text, append=False)
            started = True
            return
        pending.append(text)
        if worker is None or worker.done():
            if worker is not None:
                worker.result()
            worker = asyncio.create_task(flush_batches())
        # Backpressure when a slow event queue falls behind the producer.
        if len(pending) >= 1024:
            flush_now.set()
            await worker

    try:
        yield on_text
    finally:
        flush_now.set()
        if worker is not None:
            await worker


def _needs_json_text(value) -> bool:
    """Protobuf Value represents every JSON number as a double."""
    if isinstance(value, dict):
        return any(_needs_json_text(item) for item in value.values())
    if isinstance(value, (tuple, list)):
        return any(_needs_json_text(item) for item in value)
    return isinstance(value, (int, float)) and abs(value) >= 2**53


def _public_interrupt(item, secrets: tuple[str, ...]):
    """Project native approvals as structured data without the framework's raw description."""
    value = safe_interrupt_fields(item.value, secrets)
    if isinstance(value, dict) and {"action_requests", "review_configs"} <= value.keys():
        value = {
            **value,
            "action_requests": [
                {
                    "name": action["name"],
                    "args": json.dumps(action["args"], ensure_ascii=False)
                    if _needs_json_text(action["args"])
                    else action["args"],
                }
                for action in value["action_requests"]
            ],
        }
    if _needs_json_text(value):
        return {"id": item.id, "valueJson": json.dumps(value, ensure_ascii=False)}
    return {"id": item.id, "value": value}


def _validate_resume_value(request, value) -> None:
    """Check the known interrupt shapes before LangGraph consumes the response."""
    if not isinstance(request, dict):
        return
    if "human_feedback" in (request.get("kind"), request.get("type")) and (
        not isinstance(value, str) or not value.strip()
    ):
        raise InvalidParamsError(message="Human feedback must be non-empty text")
    if "action_requests" not in request or "review_configs" not in request:
        return

    reviews = request["review_configs"]
    decisions = value.get("decisions") if isinstance(value, dict) else None
    if not isinstance(decisions, list) or len(decisions) != len(reviews):
        raise InvalidParamsError(message="Supply one decision per requested action")

    for index, (decision, review) in enumerate(zip(decisions, reviews, strict=True)):
        kind = decision.get("type") if isinstance(decision, dict) else None
        if kind not in review["allowed_decisions"]:
            raise InvalidParamsError(message="Decision is not allowed for this action")
        if kind == "edit":
            action = decision.get("edited_action")
            if (
                not isinstance(action, dict)
                or not isinstance(action.get("name"), str)
                or not action["name"].strip()
            ):
                raise InvalidParamsError(message="An edited action needs a tool name and arguments")
            args = action.get("args")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError as error:
                    raise InvalidParamsError(
                        message="Edited arguments must be a JSON object"
                    ) from error
                action["args"] = args
            if not isinstance(args, dict):
                raise InvalidParamsError(message="Edited arguments must be a JSON object")
            shown = request["action_requests"][index]["args"]
            if "[REDACTED]" in json.dumps(shown) and "[REDACTED]" in json.dumps(args):
                raise InvalidParamsError(
                    message="Replace redacted arguments before editing, or approve the original action"
                )
        if kind == "respond" or (kind == "reject" and "message" in decision):
            if not isinstance(decision.get("message"), str):
                raise InvalidParamsError(message="The decision message must be text")


def _input(context: RequestContext) -> str | Command:
    """Keep native resume values separate from model-visible user messages."""
    parts = context.message.parts if context.message else ()
    data = [MessageToDict(part.data) for part in parts if part.HasField("data")]
    responses = [
        item
        for item in data
        if isinstance(item, dict) and item.get("kind") == "dataagent.hitl.response"
    ]
    task = context.current_task
    if not responses:
        if task is not None and task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
            raise InvalidParamsError(message="Send a structured HITL response to the waiting task")
        return (context.get_user_input() or "").strip()
    if len(parts) != 1 or task is None or task.status.state != TaskState.TASK_STATE_INPUT_REQUIRED:
        raise InvalidParamsError(message="A HITL response must target a task waiting for input")
    response = context.call_context.state.get("dataagent_hitl_response", responses[0])
    pending_parts = (
        MessageToDict(part.data) for part in task.status.message.parts if part.HasField("data")
    )
    waiting = next(
        (part for part in pending_parts if part.get("kind") == "dataagent.hitl.request"), {}
    )
    pause_id = waiting.get("pauseId")
    if not pause_id or response.get("pauseId") != pause_id:
        raise InvalidParamsError(message="A HITL response must match the current pauseId")
    values = response.get("responses")
    if "responsesJson" in response:
        if "responses" in response or not isinstance(response["responsesJson"], str):
            raise InvalidParamsError(
                message="Supply responses or a JSON object string in responsesJson"
            )
        try:
            values = json.loads(response["responsesJson"])
        except ValueError as error:
            raise InvalidParamsError(message="responsesJson must encode an object") from error
    elif _needs_json_text(values):
        raise InvalidParamsError(message="Send large numbers using responsesJson")
    pending = {
        item["id"]: json.loads(item["valueJson"]) if "valueJson" in item else item["value"]
        for item in waiting["interrupts"]
    }
    if not isinstance(values, dict) or not values or not set(values).issubset(pending):
        raise InvalidParamsError(message="HITL responses must reference pending interrupt IDs")
    for interrupt_id, value in values.items():
        _validate_resume_value(pending[interrupt_id], value)
    return Command(resume=values)


class HITLContextBuilder(SimpleRequestContextBuilder):
    """Reject invalid resume messages before the SDK starts or mutates their task."""

    def __init__(self, task_store, secrets=()):
        super().__init__()
        self.task_store = task_store
        self.secrets = tuple(secrets)

    async def build(self, *, context, params=None, task_id=None, context_id=None, task=None):
        if task is None and task_id:
            task = await self.task_store.get(task_id, context)
        if task is not None and not context_id:
            context_id = task.context_id
        request = await super().build(
            context=context,
            params=params,
            task_id=task_id,
            context_id=context_id,
            task=task,
        )
        inputs = _input(request)
        if isinstance(inputs, Command):
            # The SDK puts request.message into Task.history. Keep the execution
            # payload in this call only and give the SDK a public copy.
            part = request.message.parts[0]
            raw = MessageToDict(part.data)
            context.state["dataagent_hitl_response"] = raw
            public = safe_interrupt_fields(raw, self.secrets)
            if "responsesJson" in public:
                public["responsesJson"] = json.dumps(
                    safe_interrupt_fields(inputs.resume, self.secrets), ensure_ascii=False
                )
            for key, answer in public.get("responses", {}).items():
                if not isinstance(answer, dict) or not isinstance(answer.get("decisions"), list):
                    continue
                for index, decision in enumerate(answer["decisions"]):
                    action = decision.get("edited_action") if isinstance(decision, dict) else None
                    if isinstance(action, dict) and isinstance(action.get("args"), str):
                        args = inputs.resume[key]["decisions"][index]["edited_action"]["args"]
                        action["args"] = json.dumps(
                            safe_interrupt_fields(args, self.secrets), ensure_ascii=False
                        )
            part.data.CopyFrom(new_data_part(public).data)
        if task is not None and context.state.get("method") == "SendStreamingMessage":
            context.state["dataagent_initial_task"] = task
        return request


def message_text(message) -> str:
    """Flatten a LangChain message into the text an A2A caller can read."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts)


def final_text(result) -> str:
    """Prefer the last non-empty AI message. Fall back to the last message."""
    messages = result.get("messages") if isinstance(result, dict) else None
    if not messages:
        return str(result)
    for message in reversed(messages):
        if getattr(message, "type", None) == "ai":
            text = message_text(message).strip()
            if text:
                return text
    return message_text(messages[-1]).strip() or str(messages[-1])


class AgentKitExecutor(AgentExecutor):
    """Run each A2A context as one AgentKit thread.

    ``reply`` receives ``(thread_id, user_text_or_resume)`` and returns the graph state.
    The same context id is reused as the LangGraph ``thread_id``, so a later
    message continues that checkpoint. Calls that share a context run one at a
    time. ``secrets`` are removed from failure text.
    """

    def __init__(
        self,
        reply: Reply,
        *,
        secrets: tuple[str, ...] = (),
        cancel_waiting: Callable[[str, str], Awaitable[None]] | None = None,
        stream_reply: Callable[
            [str, str | Command, Callable[[str], Awaitable[None]]], Awaitable[object]
        ]
        | None = None,
    ):
        self._reply = reply
        self._stream_reply = stream_reply
        self._secrets = secrets
        self._cancel_waiting = cancel_waiting
        self._runs: dict[str, tuple[asyncio.Task, RequestInfo]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        context_id = context.context_id
        if not task_id or not context_id:
            raise RuntimeError("A2A request is missing a task id or context id")
        try:
            inputs = _input(context)
        except InvalidParamsError:
            if context.current_task is None:
                raise
            # The builder validated this request before it was queued. If it is now
            # stale, return without events so the SDK replies with the current Task.
            return
        retry_event = None
        if isinstance(inputs, Command):
            # Copy the pending data before the SDK applies the WORKING status.
            retry_event = new_text_status_update_event(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_INPUT_REQUIRED,
                text="The session is busy. Retry this HITL response after the active run finishes.",
            )
            retry_event.status.message.parts.extend(
                part for part in context.current_task.status.message.parts if part.HasField("data")
            )
        request = current_request.get() or RequestInfo()
        invocation = replace(
            request,
            task_id=task_id,
            context_id=context_id,
            stage="validation",
            message_id=context.message.message_id if context.message else "",
            task_metadata=(
                MessageToDict(context.current_task.metadata).get("dataagent")
                if context.current_task is not None
                else None
            ),
        )
        token = current_request.set(invocation)
        log_token = log_context.set(
            {**log_context.get(), "requestId": request.request_id, "stage": "a2a"}
        )
        started = time.monotonic()
        try:
            if context.current_task is None:
                task = new_task(
                    task_id=task_id, context_id=context_id, state=TaskState.TASK_STATE_SUBMITTED
                )
                task.metadata.update(invocation.metadata())
                await event_queue.enqueue_event(task)
            if isinstance(inputs, str) and not inputs:
                await self._fail(
                    event_queue, invocation, "No user message provided.", "BAD_REQUEST"
                )
                return
            try:
                thread_id = opaque_id(context_id, "A2A context id")
            except ValueError as error:
                await self._fail(event_queue, invocation, str(error), "BAD_REQUEST")
                return

            invocation.root_run_id = request.root_run_id = str(uuid4())
            log_context.set(
                {**log_context.get(), "threadId": thread_id, "runId": invocation.root_run_id}
            )
            invocation.stage = "queue"
            self._runs[task_id] = (asyncio.current_task(), invocation)
            audit("a2a.assembly.queued", secrets=self._secrets)
            await self._status(
                event_queue, invocation, TaskState.TASK_STATE_WORKING, "Processing request..."
            )
            artifact_id = f"{task_id}-result"
            lock = await self._lock_for(thread_id)
            async with lock:
                invocation.stage = "agent_invoke"
                audit("a2a.assembly.started", secrets=self._secrets)
                if (
                    context.call_context.state.get("method") == "SendStreamingMessage"
                    and self._stream_reply is not None
                ):
                    async with _artifact_preview(
                        event_queue, task_id, context_id, artifact_id
                    ) as on_text:
                        result = await self._stream_reply(thread_id, inputs, on_text)
                else:
                    result = await self._reply(thread_id, inputs)
            invocation.stage = "response"
            interrupts = result.get("__interrupt__", ()) if isinstance(result, dict) else ()
            if interrupts:
                await self._status(
                    event_queue,
                    invocation,
                    TaskState.TASK_STATE_INPUT_REQUIRED,
                    "Human input is required.",
                    interrupts=interrupts,
                )
                audit(
                    "a2a.assembly.input_required",
                    secrets=self._secrets,
                    interrupt_ids=[item.id for item in interrupts],
                )
                return
            answer = final_text(result)
            await event_queue.enqueue_event(
                new_text_artifact_update_event(
                    task_id=task_id,
                    context_id=context_id,
                    name="dataagent_result",
                    text=answer,
                    artifact_id=artifact_id,
                    last_chunk=True,
                )
            )
            await self._status(event_queue, invocation, TaskState.TASK_STATE_COMPLETED, answer)
            audit(
                "a2a.assembly.completed",
                secrets=self._secrets,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        except asyncio.CancelledError:
            if self._cancel_waiting is not None:
                await self._cancel_waiting(context_id, task_id)
            invocation.error = {
                "code": "CANCELLED",
                "stage": invocation.stage,
                "message": "Task was canceled.",
            }
            await self._status(
                event_queue, invocation, TaskState.TASK_STATE_CANCELED, "Task was canceled."
            )
            audit(
                "a2a.assembly.cancelled",
                secrets=self._secrets,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            raise
        except Exception as error:
            if (
                retry_event is not None
                and invocation.stage == "session_prepare"
                and isinstance(error, A2ARejection)
                and error.a2a_code == "CONFLICT"
            ):
                await event_queue.enqueue_event(retry_event)
                audit("a2a.assembly.resume_conflict", secrets=self._secrets, error_code="CONFLICT")
                return
            details = safe_error(error, self._secrets)
            await self._fail(
                event_queue,
                invocation,
                details["message"],
                getattr(error, "a2a_code", details["code"]),
                error=error,
            )
        finally:
            self._runs.pop(task_id, None)
            log_context.reset(log_token)
            current_request.reset(token)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ""
        context_id = context.context_id or ""
        run, invocation = self._runs.get(task_id, (None, None))
        audit(
            "a2a.task.cancel_requested",
            request=current_request.get(),
            secrets=self._secrets,
            task_id=task_id,
            context_id=context_id,
            root_run_id=invocation.root_run_id if invocation else None,
        )
        if run is not None:
            run.cancel()
            # The live execution publishes CANCELED after unwinding its host turn.
            await asyncio.wait((run,))
            return
        if task_id and context_id:
            if self._cancel_waiting is not None:
                await self._cancel_waiting(context_id, task_id)
            event = new_text_status_update_event(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_CANCELED,
                text="Task was canceled.",
            )
            details = MessageToDict(context.current_task.metadata) if context.current_task else {}
            if not isinstance(details.get("dataagent"), dict):
                details["dataagent"] = {}
            details["dataagent"]["error"] = {
                "code": "CANCELLED",
                "stage": "validation",
                "message": "Task was canceled.",
            }
            event.metadata.update(details)
            await event_queue.enqueue_event(event)

    async def _lock_for(self, thread_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[thread_id] = lock
            return lock

    async def _fail(self, event_queue, invocation, text: str, code: str, *, error=None) -> None:
        invocation.error = {"code": code, "message": text, "stage": invocation.stage}
        audit(
            "a2a.assembly.failed",
            request=invocation,
            secrets=self._secrets,
            level=logging.ERROR,
            error_code=code,
            **(exception_fields(error, self._secrets) if error else {"message": text}),
        )
        await self._status(event_queue, invocation, TaskState.TASK_STATE_FAILED, text)

    async def _status(self, event_queue, invocation, state, text: str, *, interrupts=()) -> None:
        event = new_text_status_update_event(
            task_id=invocation.task_id,
            context_id=invocation.context_id,
            state=state,
            text=text,
        )
        if interrupts:
            event.status.message.parts.append(
                new_data_part(
                    {
                        "kind": "dataagent.hitl.request",
                        "pauseId": invocation.root_run_id,
                        "interrupts": [
                            _public_interrupt(item, self._secrets) for item in interrupts
                        ],
                    }
                )
            )
        event.metadata.update(invocation.metadata())
        await event_queue.enqueue_event(event)
