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
import re
import uuid
from typing import Any

from a2a.helpers import (
    new_task,
    new_text_artifact,
    new_text_status_update_event,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types.a2a_pb2 import (
    TaskArtifactUpdateEvent,
    TaskState,
)
from loguru import logger

from dataagent.core.errors import DataAgentError
from dataagent.interface.sdk.agent import DataAgent
from dataagent.utils.log import dataagent_log_context

_A2A_STREAMING_METADATA_KEY = "dataagent_streaming"


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

        This implementation uses agent.astream() to enable true streaming,
        where each output chunk is converted to an A2A TaskStatusUpdateEvent
        and sent via the event queue for SSE delivery.
        """
        task = context.current_task or new_task(
            task_id=context.task_id,
            context_id=context.context_id,
            state=TaskState.TASK_STATE_SUBMITTED,
        )
        await event_queue.enqueue_event(task)

        user_text = _extract_text_from_context(context)
        session_id = context.context_id or context.task_id or f"a2a-{uuid.uuid4().hex[:12]}"
        with dataagent_log_context(session_id=session_id, agent_name="a2a", request_id=context.task_id):
            if not user_text:
                error = DataAgentError(source="config", component="a2a")
                await self._emit_failed(
                    event_queue,
                    context.task_id,
                    context.context_id,
                    f"{error.actor_text()} trace_id={error.trace_id}",
                )
                return

            cancel_event = asyncio.Event()
            self._cancellation_events[context.task_id] = cancel_event
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

                    if _is_streaming_request(context):
                        await self._execute_agent_astream(
                            user_text=user_text,
                            session_id=session_id,
                            task_id=context.task_id,
                            context_id=context.context_id,
                            event_queue=event_queue,
                            cancel_event=cancel_event,
                        )
                    else:
                        await self._execute_agent_chat(
                            user_text=user_text,
                            session_id=session_id,
                            task_id=context.task_id,
                            context_id=context.context_id,
                            event_queue=event_queue,
                            cancel_event=cancel_event,
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
                error = e if isinstance(e, DataAgentError) else DataAgentError.from_exception(e, component="a2a")
                logger.exception(
                    "DataAgent execution failed source={} trace_id={}",
                    error.source,
                    error.trace_id,
                )
                await event_queue.enqueue_event(
                    new_text_status_update_event(
                        task_id=context.task_id,
                        context_id=context.context_id,
                        state=TaskState.TASK_STATE_FAILED,
                        text=f"{error.actor_text()} trace_id={error.trace_id}",
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

    async def _build_initial_state(self, session_id: str, user_text: str) -> dict[str, Any]:
        """Build the per-turn DataAgent initial state and advance session run_id."""
        async with self._run_lock:
            run_id = self._run_counters.get(session_id, 0)
            self._run_counters[session_id] = run_id + 1
        return {
            "session_id": session_id,
            "run_id": run_id,
            "user_query": user_text,
        }

    async def _emit_canceled(
        self,
        event_queue: EventQueue,
        task_id: str,
        context_id: str | None,
    ) -> None:
        """Emit a CANCELED status event."""
        await event_queue.enqueue_event(
            new_text_status_update_event(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_CANCELED,
                text="Task was canceled.",
            )
        )

    async def _emit_failed(
        self,
        event_queue: EventQueue,
        task_id: str,
        context_id: str | None,
        text: str,
    ) -> None:
        """Emit a FAILED status event."""
        await event_queue.enqueue_event(
            new_text_status_update_event(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_FAILED,
                text=text,
            )
        )

    async def _emit_final_state(
        self,
        event_queue: EventQueue,
        task_id: str,
        context_id: str | None,
        final_state: Any,
        fallback_text: str = "",
    ) -> None:
        """Convert a DataAgent final state into A2A artifact and terminal status events."""
        final_text = fallback_text.strip() if fallback_text else _extract_final_answer(final_state).strip()
        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                artifact=new_text_artifact(name="dataagent_result", text=final_text),
            )
        )
        await event_queue.enqueue_event(
            new_text_status_update_event(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_COMPLETED,
                text=final_text,
            )
        )

    async def _execute_agent_astream(
        self,
        user_text: str,
        session_id: str,
        task_id: str,
        context_id: str | None,
        event_queue: EventQueue,
        cancel_event: asyncio.Event,
    ) -> None:
        """Execute agent with streaming, sending each chunk as an A2A event.

        This method wraps agent.astream() to provide true streaming support.
        Each output chunk is sent via the event queue as a TaskStatusUpdateEvent,
        which is then delivered to the client via SSE.

        Args:
            user_text: The user's input text.
            session_id: The session ID for conversation context.
            task_id: The A2A task ID.
            context_id: The A2A context ID.
            event_queue: The event queue for sending A2A events.
            cancel_event: Event to signal cancellation.
        """
        initial_state = await self._build_initial_state(session_id, user_text)

        try:
            # Use DataAgent's high-level astream path. Passing only initial_state keeps
            # Flex and NL2SQL on their shared SDK contract instead of mixing it with
            # LangGraph's native input=... path.
            stream_response = self._agent.astream(
                session_id=session_id,
                initial_state=initial_state,
                stream_mode=["updates", "custom", "values"],
            )

            # Track incremental deltas separately from full state snapshots
            stream_text = ""  # For custom/delta chunks (use +=)
            latest_state_text = ""  # For values/updates snapshots (use assignment)
            final_state: dict[str, Any] = {}  # Track the latest state for structured error checks
            chunk_count = 0

            async for chunk in stream_response:
                chunk_count += 1
                logger.debug(
                    f"[A2A Streaming] Chunk #{chunk_count}: type={type(chunk).__name__}, value={repr(chunk)[:500]}"
                )

                if cancel_event.is_set():
                    await self._emit_canceled(event_queue, task_id, context_id)
                    return

                # Unpack the chunk to determine mode and data
                mode, data = self._unpack_stream_chunk(chunk)

                if mode == "custom" and isinstance(data, dict):
                    # Custom mode: treat as incremental delta, use +=
                    delta = self._extract_custom_delta_text(data)
                    if delta:
                        # Append to the current text (snapshot or previous deltas)
                        stream_text += delta
                        latest_state_text = stream_text
                        await self._emit_working(event_queue, task_id, context_id, stream_text)

                elif mode in ("values", "updates") and isinstance(data, dict):
                    # Values/updates mode: treat as full state snapshot
                    # Snapshot replaces all previous content (including deltas)
                    final_state = data
                    snapshot = self._extract_state_answer_text(data)
                    if snapshot:
                        stream_text = snapshot
                        latest_state_text = snapshot
                        await self._emit_working(event_queue, task_id, context_id, snapshot)

                elif mode == "unknown" and isinstance(data, str) and data.strip():
                    # Raw string chunks: treat as incremental delta
                    stream_text += data
                    latest_state_text = stream_text
                    await self._emit_working(event_queue, task_id, context_id, stream_text)

            # Use the final state snapshot as primary text, fallback to stream_text
            final_text = latest_state_text or stream_text
            await self._emit_final_state(event_queue, task_id, context_id, final_state, fallback_text=final_text)

        except asyncio.CancelledError:
            await self._emit_canceled(event_queue, task_id, context_id)
            return
        except Exception as e:
            error = e if isinstance(e, DataAgentError) else DataAgentError.from_exception(e, component="a2a")
            logger.exception(
                "Streaming execution failed source={} trace_id={}",
                error.source,
                error.trace_id,
            )
            await self._emit_failed(event_queue, task_id, context_id, f"{error.actor_text()} trace_id={error.trace_id}")

    async def _execute_agent_chat(
        self,
        user_text: str,
        session_id: str,
        task_id: str,
        context_id: str | None,
        event_queue: EventQueue,
        cancel_event: asyncio.Event,
    ) -> None:
        """Execute agent with chat for A2A non-streaming requests."""
        initial_state = await self._build_initial_state(session_id, user_text)

        try:
            if cancel_event.is_set():
                await self._emit_canceled(event_queue, task_id, context_id)
                return

            final_state = await self._agent.chat(
                user_text,
                session_id=session_id,
                initial_state=initial_state,
            )

            if cancel_event.is_set():
                await self._emit_canceled(event_queue, task_id, context_id)
                return

            await self._emit_final_state(event_queue, task_id, context_id, final_state)
        except asyncio.CancelledError:
            await self._emit_canceled(event_queue, task_id, context_id)
            return
        except Exception as e:
            error = e if isinstance(e, DataAgentError) else DataAgentError.from_exception(e, component="a2a")
            logger.exception(
                "Chat execution failed source={} trace_id={}",
                error.source,
                error.trace_id,
            )
            await self._emit_failed(event_queue, task_id, context_id, f"{error.actor_text()} trace_id={error.trace_id}")

    def _clean_chunk_text(self, text: str) -> str:
        """Clean up chunk text by removing common prefixes and formatting.

        FlexAgent streaming often includes prefixes like '**planner:**' which
        should be stripped for cleaner output.

        Args:
            text: Raw text from chunk.

        Returns:
            Cleaned text.
        """
        if not text:
            return ""

        # Remove common streaming prefixes
        # Pattern: **node_name:** at the start of text, possibly followed by newlines
        text = re.sub(r"^\*\*[a-zA-Z_]+\*\*:\s*", "", text)
        text = re.sub(r"^\*\*[a-zA-Z_]+\*\*\n+", "", text)

        # Remove repeated newlines at the start
        text = re.sub(r"^\n+", "", text)

        return text

    def _extract_chunk_text(self, chunk: Any) -> str:
        """Extract text content from various FlexAgent chunk formats.

        Handles:
        - dict with 'content' or 'text' key
        - tuple (mode, data) from langgraph streaming
        - tuple (_, mode, data) from langgraph streaming
        - AIMessage or similar with content attribute
        - string directly

        Returns:
            Extracted text content or empty string.
        """
        if chunk is None:
            return ""

        # Handle string directly
        if isinstance(chunk, str):
            text = self._clean_chunk_text(chunk)
            return text

        # Handle dict with content/text
        if isinstance(chunk, dict):
            # Check common keys for text content
            for key in ("content", "text", "answer", "final_answer", "result", "response"):
                if key in chunk:
                    value = chunk[key]
                    if isinstance(value, str) and value.strip():
                        return self._clean_chunk_text(value)
            return ""

        # Handle tuple format: (mode, data) or (_, mode, data)
        if isinstance(chunk, tuple):
            if len(chunk) == 2:
                mode, data = chunk
            elif len(chunk) == 3:
                _, mode, data = chunk
            else:
                return ""

            # Process data based on mode
            if mode in ("values", "updates") and isinstance(data, dict):
                # Try to extract text from state dict
                for key in ("content", "text", "answer", "final_answer"):
                    if key in data:
                        value = data[key]
                        if isinstance(value, str) and value.strip():
                            return self._clean_chunk_text(value)
                # Try to extract from messages
                messages = data.get("messages", [])
                if messages and isinstance(messages, list):
                    last_msg = messages[-1]
                    content = getattr(last_msg, "content", None) if hasattr(last_msg, "content") else None
                    if isinstance(content, str) and content.strip():
                        return self._clean_chunk_text(content)
                    if isinstance(content, list):
                        # Handle content blocks (common in newer langchain)
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "")
                                if isinstance(text, str) and text.strip():
                                    return self._clean_chunk_text(text)
                            elif hasattr(block, "text"):
                                text = block.text
                                if isinstance(text, str) and text.strip():
                                    return self._clean_chunk_text(text)
            elif mode == "custom" and isinstance(data, dict):
                # Custom mode often contains streaming tokens
                for key in ("content", "text", "token"):
                    if key in data:
                        value = data[key]
                        if isinstance(value, str) and value.strip():
                            return self._clean_chunk_text(value)
            return ""

        # Handle objects with content attribute (e.g., AIMessage)
        if hasattr(chunk, "content"):
            content = chunk.content
            if isinstance(content, str) and content.strip():
                return self._clean_chunk_text(content)
            if isinstance(content, list):
                # Handle content blocks
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if isinstance(text, str) and text.strip():
                            return self._clean_chunk_text(text)
                    elif hasattr(block, "text"):
                        text = block.text
                        if isinstance(text, str) and text.strip():
                            return self._clean_chunk_text(text)
            return ""

        # Fallback: try to get any text-like attribute
        for attr_name in ("output", "result", "value", "message", "data"):
            if hasattr(chunk, attr_name):
                attr_val = getattr(chunk, attr_name)
                if isinstance(attr_val, str) and attr_val.strip():
                    return self._clean_chunk_text(attr_val)
                if isinstance(attr_val, dict):
                    for key in ("text", "content", "value"):
                        if key in attr_val:
                            val = attr_val[key]
                            if isinstance(val, str) and val.strip():
                                return self._clean_chunk_text(val)

        # Last resort: try str() conversion if chunk is not empty
        chunk_str = str(chunk)
        if chunk_str and chunk_str not in ("None", "()", "[]", "{}"):
            logger.debug(f"[A2A Streaming] Fallback str() conversion: {chunk_str[:200]}")
            return self._clean_chunk_text(chunk_str)

        return ""

    def _unpack_stream_chunk(self, chunk: Any) -> tuple[str, Any]:
        """Unpack a streaming chunk to extract mode and data.

        Handles various chunk formats from FlexAgent/LangGraph:
        - tuple (mode, data)
        - tuple (_, mode, data)
        - dict with 'type' or 'mode' key
        - other formats (returns ("unknown", chunk))

        Returns:
            Tuple of (mode, data).
        """
        if isinstance(chunk, tuple):
            if len(chunk) == 2:
                mode, data = chunk
                return (str(mode) if mode else "unknown", data)
            elif len(chunk) == 3:
                _, mode, data = chunk
                return (str(mode) if mode else "unknown", data)

        if isinstance(chunk, dict):
            mode = chunk.get("type") or chunk.get("mode") or "unknown"
            return (str(mode), chunk)

        return ("unknown", chunk)

    def _extract_custom_delta_text(self, data: dict[str, Any]) -> str:
        """Extract incremental delta text from custom event data.

        Custom events typically contain streaming tokens that should be
        appended (not replaced).

        Args:
            data: The data dict from a custom event.

        Returns:
            Extracted delta text or empty string.
        """
        if not isinstance(data, dict):
            return ""

        for key in ("content", "text", "token", "delta"):
            if key in data:
                value = data[key]
                if isinstance(value, str) and value.strip():
                    return self._clean_chunk_text(value)

        return ""

    def _extract_state_answer_text(self, data: dict[str, Any]) -> str:
        """Extract the answer text from a full state snapshot (values/updates mode).

        This should be used for values/updates mode where we get full state
        snapshots, not incremental deltas. The returned text should be used
        with assignment, not +=.

        Args:
            data: The state dict from a values/updates event.

        Returns:
            Extracted answer text or empty string.
        """
        if not isinstance(data, dict):
            return ""

        # Try direct answer keys first
        for key in ("content", "text", "answer", "final_answer", "result", "response"):
            if key in data:
                value = data[key]
                if isinstance(value, str) and value.strip():
                    return self._clean_chunk_text(value)

        # Try to extract from messages
        messages = data.get("messages", [])
        if messages and isinstance(messages, list):
            last_msg = messages[-1]
            if isinstance(last_msg, dict):
                content = last_msg.get("content")
                if isinstance(content, str) and content.strip():
                    return self._clean_chunk_text(content)
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if isinstance(text, str) and text.strip():
                                return self._clean_chunk_text(text)
            else:
                content = getattr(last_msg, "content", None)
                if isinstance(content, str) and content.strip():
                    return self._clean_chunk_text(content)
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if isinstance(text, str) and text.strip():
                                return self._clean_chunk_text(text)
                        elif hasattr(block, "text"):
                            text = block.text
                            if isinstance(text, str) and text.strip():
                                return self._clean_chunk_text(text)

        return ""

    async def _emit_working(
        self,
        event_queue: EventQueue,
        task_id: str,
        context_id: str | None,
        text: str,
    ) -> None:
        """Emit a WORKING status update with the current text.

        Args:
            event_queue: The event queue for sending A2A events.
            task_id: The A2A task ID.
            context_id: The A2A context ID.
            text: Current accumulated text to send.
        """
        await event_queue.enqueue_event(
            new_text_status_update_event(
                task_id=task_id,
                context_id=context_id,
                state=TaskState.TASK_STATE_WORKING,
                text=text,
            )
        )

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


def _is_streaming_request(context: RequestContext) -> bool:
    """Return whether this A2A request should use DataAgent.astream()."""
    metadata = context.metadata
    marker = metadata.get(_A2A_STREAMING_METADATA_KEY, True)
    if isinstance(marker, bool):
        return marker
    if isinstance(marker, str):
        return marker.strip().lower() not in {"0", "false", "no"}
    return bool(marker)


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
