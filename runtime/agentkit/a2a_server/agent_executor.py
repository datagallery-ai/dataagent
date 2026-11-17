"""Bridge one A2A 1.0 task to a single AgentKit graph invocation."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from uuid import uuid4

from a2a.helpers import (
    new_task,
    new_text_artifact_update_event,
    new_text_status_update_event,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types.a2a_pb2 import TaskState

from a2a_server.request_context import (
    RequestInfo,
    audit,
    current_request,
    exception_fields,
)
from dataagent.bootstrap.paths import opaque_id
from dataagent.diagnostics import safe_error

Reply = Callable[[str, str], Awaitable[object]]


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

    ``reply`` receives ``(thread_id, user_text)`` and returns the graph state.
    The same context id is reused as the LangGraph ``thread_id``, so a later
    message continues that checkpoint. Calls that share a context run one at a
    time. ``secrets`` are removed from failure text.
    """

    def __init__(self, reply: Reply, *, secrets: tuple[str, ...] = ()):
        self._reply = reply
        self._secrets = secrets
        self._runs: dict[str, tuple[asyncio.Task, RequestInfo]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        context_id = context.context_id
        if not task_id or not context_id:
            raise RuntimeError("A2A request is missing a task id or context id")
        request = current_request.get() or RequestInfo()
        invocation = replace(
            request, task_id=task_id, context_id=context_id, stage="validation",
            message_id=context.message.message_id if context.message else "",
        )
        token = current_request.set(invocation)
        started = time.monotonic()
        try:
            task = new_task(task_id=task_id, context_id=context_id,
                            state=TaskState.TASK_STATE_SUBMITTED)
            task.metadata.update(invocation.metadata())
            await event_queue.enqueue_event(task)
            user_text = (context.get_user_input() or "").strip()
            if not user_text:
                await self._fail(event_queue, invocation, "No user message provided.", "BAD_REQUEST")
                return
            try:
                thread_id = opaque_id(context_id, "A2A context id")
            except ValueError as error:
                await self._fail(event_queue, invocation, str(error), "BAD_REQUEST")
                return

            invocation.root_run_id = str(uuid4())
            invocation.stage = "queue"
            self._runs[task_id] = (asyncio.current_task(), invocation)
            audit("a2a.execution.queued", secrets=self._secrets)
            await self._status(event_queue, invocation, TaskState.TASK_STATE_WORKING,
                               "Processing request...")
            lock = await self._lock_for(thread_id)
            async with lock:
                invocation.stage = "agent_invoke"
                audit("a2a.execution.started", secrets=self._secrets)
                result = await self._reply(thread_id, user_text)
            invocation.stage = "response"
            answer = final_text(result)
            await event_queue.enqueue_event(
                new_text_artifact_update_event(
                    task_id=task_id,
                    context_id=context_id,
                    name="dataagent_result",
                    text=answer,
                    last_chunk=True,
                )
            )
            await self._status(event_queue, invocation, TaskState.TASK_STATE_COMPLETED, answer)
            audit("a2a.execution.completed", secrets=self._secrets,
                  duration_ms=round((time.monotonic() - started) * 1000))
        except asyncio.CancelledError:
            invocation.error = {"code": "CANCELLED", "stage": invocation.stage,
                                "message": "Task was canceled."}
            await self._status(event_queue, invocation, TaskState.TASK_STATE_CANCELED,
                               "Task was canceled.")
            audit("a2a.execution.cancelled", secrets=self._secrets,
                  duration_ms=round((time.monotonic() - started) * 1000))
            raise
        except Exception as error:
            details = safe_error(error, self._secrets)
            await self._fail(
                event_queue, invocation, details["message"],
                getattr(error, "a2a_code", details["code"]), error=error,
            )
        finally:
            self._runs.pop(task_id, None)
            current_request.reset(token)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ""
        context_id = context.context_id or ""
        run, invocation = self._runs.get(task_id, (None, None))
        audit("a2a.task.cancel_requested", request=current_request.get(), secrets=self._secrets,
              task_id=task_id, context_id=context_id,
              root_run_id=invocation.root_run_id if invocation else None)
        if run is not None:
            run.cancel()
        if task_id and context_id:
            event = new_text_status_update_event(
                task_id=task_id, context_id=context_id, state=TaskState.TASK_STATE_CANCELED,
                text="Task was canceled.",
            )
            if invocation is not None:
                details = invocation.metadata()
                stage = invocation.stage
            else:
                from google.protobuf.json_format import MessageToDict

                details = MessageToDict(context.current_task.metadata) if context.current_task else {}
                stage = "validation"
            if not isinstance(details.get("dataagent"), dict):
                details["dataagent"] = {}
            details["dataagent"]["error"] = {
                "code": "CANCELLED", "stage": stage, "message": "Task was canceled.",
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
        audit("a2a.execution.failed", request=invocation, secrets=self._secrets,
              level=logging.ERROR, error_code=code,
              **(exception_fields(error, self._secrets) if error else {"message": text}))
        await self._status(event_queue, invocation, TaskState.TASK_STATE_FAILED, text)

    async def _status(self, event_queue, invocation, state, text: str) -> None:
        event = new_text_status_update_event(
            task_id=invocation.task_id, context_id=invocation.context_id, state=state, text=text,
        )
        event.metadata.update(invocation.metadata())
        await event_queue.enqueue_event(event)
