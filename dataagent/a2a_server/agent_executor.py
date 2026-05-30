# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""DataAgentExecutor — bridges A2A 1.0 requests to DataAgent."""

import asyncio
import uuid
from typing import Any

from a2a.helpers import (
    new_task,
    new_text_artifact,
    new_text_message,
    new_text_status_update_event,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types.a2a_pb2 import (
    Role,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from loguru import logger

from dataagent.interface.sdk.agent import DataAgent


class DataAgentExecutor(AgentExecutor):
    """Bridges A2A 1.0 protocol requests to DataAgent execution."""

    def __init__(self, agent: DataAgent):
        self._agent = agent
        self._cancellation_events: dict[str, asyncio.Event] = {}
        self._run_counters: dict[str, int] = {}
        self._run_lock = asyncio.Lock()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_locks_guard = asyncio.Lock()

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        """Handle an A2A request by invoking the DataAgent.

        Publishes task status updates and artifacts to the event queue,
        supporting both sync and streaming (SSE) responses.
        """
        task = context.current_task or new_task(
            task_id=context.task_id,
            context_id=context.context_id,
            state=TaskState.TASK_STATE_SUBMITTED,
        )
        await event_queue.enqueue_event(task)

        user_text = _extract_text_from_context(context)
        if not user_text:
            await self._fail_with_message(event_queue, context, "No user message provided.")
            return

        cancel_event = asyncio.Event()
        self._cancellation_events[context.task_id] = cancel_event

        session_id = context.context_id or context.task_id or f"a2a-{uuid.uuid4().hex[:12]}"
        session_lock = await self._get_session_lock(session_id)

        try:
            async with session_lock:
                await event_queue.enqueue_event(
                    new_text_status_update_event(
                        task_id=context.task_id,
                        context_id=context.context_id,
                        state=TaskState.TASK_STATE_WORKING,
                        text="Processing request...",
                    )
                )

                response = await self._execute_agent_chat(user_text, session_id, context.task_id)

                if cancel_event.is_set():
                    await event_queue.enqueue_event(
                        new_text_status_update_event(
                            task_id=context.task_id,
                            context_id=context.context_id,
                            state=TaskState.TASK_STATE_CANCELED,
                            text="Task was canceled.",
                        )
                    )
                    return

                result_text = _extract_final_answer(response)

                has_error = self._has_error_response(response)
                if has_error:
                    error_msg = self._extract_error_message(response)
                    logger.error(f"Agent execution failed: {error_msg}")
                    await event_queue.enqueue_event(
                        new_text_status_update_event(
                            task_id=context.task_id,
                            context_id=context.context_id,
                            state=TaskState.TASK_STATE_FAILED,
                            text=str(error_msg) if error_msg else "Agent execution failed",
                        )
                    )
                    return

                logger.info("Agent execution completed successfully")

                await event_queue.enqueue_event(
                    TaskArtifactUpdateEvent(
                        task_id=context.task_id,
                        context_id=context.context_id,
                        artifact=new_text_artifact(name="dataagent_result", text=result_text),
                    )
                )
                await event_queue.enqueue_event(
                    new_text_status_update_event(
                        task_id=context.task_id,
                        context_id=context.context_id,
                        state=TaskState.TASK_STATE_COMPLETED,
                        text=result_text,
                    )
                )

        except asyncio.CancelledError:
            await event_queue.enqueue_event(
                new_text_status_update_event(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    state=TaskState.TASK_STATE_CANCELED,
                    text="Task was canceled.",
                )
            )
        except Exception as e:
            logger.error(f"DataAgent execution failed: {e}")
            await event_queue.enqueue_event(
                new_text_status_update_event(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    state=TaskState.TASK_STATE_FAILED,
                    text=f"Error: {str(e)}",
                )
            )
        finally:
            self._cancellation_events.pop(context.task_id, None)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Request cancellation of an ongoing task."""
        cancel_event = self._cancellation_events.get(context.task_id)
        if cancel_event:
            cancel_event.set()
        await event_queue.enqueue_event(
            new_text_status_update_event(
                task_id=context.task_id,
                context_id=context.context_id,
                state=TaskState.TASK_STATE_CANCELED,
                text="Task was canceled.",
            )
        )

    async def _fail_with_message(
        self,
        event_queue: EventQueue,
        context: RequestContext,
        message: str,
    ) -> None:
        """Publish a failed status event with the given message."""
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_FAILED,
                    message=new_text_message(
                        text=message,
                        role=Role.ROLE_AGENT,
                        task_id=context.task_id,
                        context_id=context.context_id,
                    ),
                ),
            )
        )

    async def _execute_agent_chat(
        self,
        user_text: str,
        session_id: str,
        task_id: str,
    ) -> Any:
        """Acquire session lock, increment run counter, and execute agent chat."""
        async with self._run_lock:
            run_id = self._run_counters.get(session_id, 0)
            self._run_counters[session_id] = run_id + 1
        initial_state = {"session_id": session_id, "run_id": run_id}

        response = await self._agent.chat(user_query=user_text, session_id=session_id, initial_state=initial_state)

        logger.info(f"[DEBUG] _agent.chat() returned type: {type(response)}")
        if isinstance(response, dict):
            logger.info(f"[DEBUG] _agent.chat() response keys: {list(response.keys())}")
            logger.info(f"[DEBUG] _agent.chat() response: {response}")

        return response

    def _has_error_response(self, response: Any) -> bool:
        """Check if the response contains an error.

        Errors can be in two places:
        1. Top-level: response.get("error") - from DataAgent.chat() exception handling
        2. Nested: response["messages"][-1].additional_kwargs.get("error") - from FlexAgent planner
        """
        if not isinstance(response, dict):
            return False

        if "error" in response:
            return True

        if response.get("messages"):
            last_msg = response["messages"][-1]
            if isinstance(last_msg, dict):
                return bool(last_msg.get("additional_kwargs", {}).get("error"))
            return bool(getattr(last_msg, "additional_kwargs", {}).get("error"))

        return False

    def _extract_error_message(self, response: Any) -> str:
        """Extract the error message from a response."""
        if not isinstance(response, dict):
            return str(response)

        if "error" in response:
            return str(response.get("error"))

        if response.get("messages"):
            last_msg = response["messages"][-1]
            if isinstance(last_msg, dict):
                return str(last_msg.get("content", str(response)))
            return str(getattr(last_msg, "content", str(response)))

        return str(response)

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Get or create a per-session Lock for FIFO task serialization."""
        async with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock
            return lock


def _extract_text_from_context(context: RequestContext) -> str:
    """Extract text content from the request context message."""
    # RequestContext.get_user_input() already handles text extraction
    user_text = context.get_user_input()
    if user_text:
        return user_text

    # Fallback: extract from context.message directly
    message = context.message
    if message is None:
        return ""

    parts_text = []
    for part in message.parts:
        if part.text:
            parts_text.append(part.text)
        elif part.data:
            try:
                import json

                from google.protobuf.json_format import MessageToDict

                data_dict = MessageToDict(part.data)
                parts_text.append(json.dumps(data_dict, ensure_ascii=False))
            except Exception:
                parts_text.append(str(part.data))

    return "".join(parts_text).strip()


def _extract_final_answer(response: Any) -> str:
    """Extract the final answer text from a DataAgent response."""
    if isinstance(response, dict):
        for key in ("final_answer", "answer", "result", "response"):
            if key in response:
                val = response[key]
                if isinstance(val, str):
                    return val
                return str(val)

        messages = response.get("messages", [])
        if messages:
            last_msg = messages[-1]
            content = getattr(last_msg, "content", str(last_msg))
            return str(content)

        return str(response)
    return str(response)
