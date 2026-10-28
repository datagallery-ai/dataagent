"""Bridge one A2A 1.0 task to a single AgentKit graph invocation."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from a2a.helpers import (
    new_task,
    new_text_artifact_update_event,
    new_text_status_update_event,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types.a2a_pb2 import TaskState

from dataagent.bootstrap.paths import opaque_id
from dataagent.diagnostics import safe_error

logger = logging.getLogger(__name__)

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
        self._runs: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        context_id = context.context_id
        if not task_id or not context_id:
            raise RuntimeError("A2A request is missing a task id or context id")
        await event_queue.enqueue_event(
            new_task(task_id=task_id, context_id=context_id, state=TaskState.TASK_STATE_SUBMITTED)
        )
        user_text = (context.get_user_input() or "").strip()
        if not user_text:
            await self._fail(event_queue, task_id, context_id, "No user message provided.")
            return
        try:
            thread_id = opaque_id(context_id, "A2A context id")
        except ValueError as error:
            await self._fail(event_queue, task_id, context_id, str(error))
            return

        self._runs[task_id] = asyncio.current_task()
        try:
            await event_queue.enqueue_event(
                new_text_status_update_event(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_WORKING,
                    text="Processing request...",
                )
            )
            lock = await self._lock_for(thread_id)
            async with lock:
                result = await self._reply(thread_id, user_text)
            answer = final_text(result)
            await event_queue.enqueue_event(
                new_text_artifact_update_event(
                    task_id=task_id,
                    context_id=context_id,
                    name="agentkit_result",
                    text=answer,
                    last_chunk=True,
                )
            )
            await event_queue.enqueue_event(
                new_text_status_update_event(
                    task_id=task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_COMPLETED,
                    text=answer,
                )
            )
        except asyncio.CancelledError:
            await self._status(
                event_queue, task_id, context_id, TaskState.TASK_STATE_CANCELED, "Task was canceled."
            )
            raise
        except Exception as error:
            logger.exception("A2A invocation failed")
            await self._fail(
                event_queue,
                task_id,
                context_id,
                safe_error(error, self._secrets)["message"],
            )
        finally:
            self._runs.pop(task_id, None)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ""
        context_id = context.context_id or ""
        run = self._runs.get(task_id)
        if run is not None:
            run.cancel()
        if task_id and context_id:
            await self._status(
                event_queue, task_id, context_id, TaskState.TASK_STATE_CANCELED, "Task was canceled."
            )

    async def _lock_for(self, thread_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[thread_id] = lock
            return lock

    async def _fail(self, event_queue, task_id: str, context_id: str, text: str) -> None:
        await self._status(event_queue, task_id, context_id, TaskState.TASK_STATE_FAILED, text)

    async def _status(self, event_queue, task_id: str, context_id: str, state, text: str) -> None:
        await event_queue.enqueue_event(
            new_text_status_update_event(
                task_id=task_id, context_id=context_id, state=state, text=text
            )
        )
