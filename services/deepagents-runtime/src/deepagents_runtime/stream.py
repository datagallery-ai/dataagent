from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from langgraph.types import Command

from deepagents_runtime.events import (
    build_interrupt_event,
    run_error,
    run_finished,
    run_started,
    runtime_bound,
    text_message_content,
    text_message_end,
    text_message_start,
    tool_call_args,
    tool_call_end,
    tool_call_result,
    tool_call_start,
)
from deepagents_runtime.messages import chunk_text, resume_message, to_langchain_messages
from deepagents_runtime.models import RuntimeRunRequest


@dataclass
class StreamState:
    thread_id: str
    run_id: str
    message_id: str | None = None
    text_open: bool = False
    started_tools: set[str] = field(default_factory=set)
    ended_tools: set[str] = field(default_factory=set)
    tool_names: dict[str, str] = field(default_factory=dict)
    unbound_by_name: dict[str, list[str]] = field(default_factory=dict)
    execution_to_tool_id: dict[str, str] = field(default_factory=dict)
    emitted_args: set[str] = field(default_factory=set)
    emitted_text: bool = False
    interrupted: bool = False


def unfinished_tool_ids(state: StreamState) -> list[str]:
    return [tool_id for tool_id in state.tool_names if tool_id not in state.ended_tools]


def _register_tool_start(state: StreamState, tool_id: str, tool_name: str, *, awaiting_execution: bool = True) -> bool:
    if not tool_id or tool_id in state.started_tools:
        return False
    state.started_tools.add(tool_id)
    state.tool_names[tool_id] = tool_name
    if awaiting_execution:
        state.unbound_by_name.setdefault(tool_name, []).append(tool_id)
    return True


def _bind_execution(state: StreamState, tool_name: str, execution_id: str) -> str | None:
    queued = state.unbound_by_name.get(tool_name) or []
    if queued:
        tool_id = queued.pop(0)
        if execution_id:
            state.execution_to_tool_id[execution_id] = tool_id
        return tool_id
    if execution_id and execution_id in state.started_tools:
        return execution_id
    return None


def _resolve_execution(state: StreamState, tool_name: str, execution_id: str) -> str | None:
    if execution_id and execution_id in state.execution_to_tool_id:
        return state.execution_to_tool_id[execution_id]
    bound = _bind_execution(state, tool_name, execution_id)
    if bound:
        return bound
    return next(
        (tool_id for tool_id, name in state.tool_names.items() if name == tool_name and tool_id not in state.ended_tools),
        None,
    )


def now_ms() -> int:
    return int(time.time() * 1000)


def checkpoint_ref_for(thread_id: str) -> str:
    return f"thread:{thread_id}"


def build_graph_input(request: RuntimeRunRequest) -> Any:
    if request.resume is None:
        messages = to_langchain_messages(request.messages)
        if not messages:
            raise ValueError("messages must not be empty")
        return {"messages": messages}

    if request.resume.interrupt.type == "mastra_suspend":
        raise ValueError("LEGACY_RUNTIME_SUSPEND_UNRECOVERABLE")

    if request.resume.response is False:
        return Command(
            resume={
                "decisions": [
                    {
                        "type": "reject",
                        "message": "User cancelled this interruption.",
                    }
                ]
            }
        )

    return Command(
        resume={
            "decisions": [
                {
                    "type": "respond",
                    "message": resume_message(request.resume.response),
                }
            ]
        }
    )


def _close_text(state: StreamState, timestamp: int) -> list[dict[str, Any]]:
    if not state.text_open or not state.message_id:
        return []
    state.text_open = False
    return [text_message_end(state.message_id, timestamp=timestamp)]


def _ensure_text(state: StreamState, timestamp: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if state.message_id is None:
        state.message_id = f"msg_{state.run_id}"
    if not state.text_open:
        events.append(text_message_start(state.message_id, timestamp=timestamp))
        state.text_open = True
    return events


def _stringify_tool_output(output: Any) -> str:
    content = getattr(output, "content", output)
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except TypeError:
        return str(content)


def map_stream_event(event: dict[str, Any], state: StreamState, *, timestamp: int) -> list[dict[str, Any]]:
    kind = event.get("event")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    events: list[dict[str, Any]] = []

    if kind == "on_chat_model_stream":
        chunk = data.get("chunk")
        text = chunk_text(getattr(chunk, "content", ""))
        if text:
            events.extend(_ensure_text(state, timestamp))
            events.append(text_message_content(state.message_id or f"msg_{state.run_id}", text, timestamp=timestamp))
            state.emitted_text = True
        for tool_chunk in getattr(chunk, "tool_call_chunks", None) or []:
            if not isinstance(tool_chunk, dict):
                continue
            tool_id = str(tool_chunk.get("id") or "")
            tool_name = str(tool_chunk.get("name") or state.tool_names.get(tool_id) or "tool")
            args = tool_chunk.get("args")
            if _register_tool_start(state, tool_id, tool_name):
                events.extend(_close_text(state, timestamp))
                events.append(tool_call_start(tool_id, tool_name, timestamp=timestamp))
            if tool_id and args:
                delta = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
                events.append(tool_call_args(tool_id, delta, timestamp=timestamp))
                state.emitted_args.add(tool_id)
        return events

    if kind == "on_chat_model_end":
        message = data.get("output") or data.get("message")
        text = chunk_text(getattr(message, "content", ""))
        if text and not state.emitted_text:
            events.extend(_ensure_text(state, timestamp))
            events.append(text_message_content(state.message_id or f"msg_{state.run_id}", text, timestamp=timestamp))
            state.emitted_text = True
        events.extend(_close_text(state, timestamp))
        for call in getattr(message, "tool_calls", None) or []:
            if not isinstance(call, dict):
                continue
            tool_id = str(call.get("id") or "")
            tool_name = str(call.get("name") or "tool")
            if not _register_tool_start(state, tool_id, tool_name):
                continue
            events.append(tool_call_start(tool_id, tool_name, timestamp=timestamp))
            args = call.get("args")
            if args:
                events.append(tool_call_args(tool_id, json.dumps(args, ensure_ascii=False), timestamp=timestamp))
                state.emitted_args.add(tool_id)
        return events

    if kind == "on_tool_start":
        tool_name = str(event.get("name") or "tool")
        execution_id = str(data.get("id") or event.get("run_id") or "")
        tool_id = _bind_execution(state, tool_name, execution_id)
        if tool_id is None:
            tool_id = execution_id or f"tool_{tool_name}_{state.run_id}"
            if _register_tool_start(state, tool_id, tool_name, awaiting_execution=False):
                events.extend(_close_text(state, timestamp))
                events.append(tool_call_start(tool_id, tool_name, timestamp=timestamp))
                if execution_id:
                    state.execution_to_tool_id[execution_id] = tool_id
        args = data.get("input")
        if tool_id and args is not None and tool_id not in state.emitted_args:
            events.append(
                tool_call_args(
                    tool_id,
                    args if isinstance(args, str) else json.dumps(args, ensure_ascii=False),
                    timestamp=timestamp,
                )
            )
            state.emitted_args.add(tool_id)
        return events

    if kind == "on_tool_end":
        tool_name = str(event.get("name") or "tool")
        execution_id = str(event.get("run_id") or data.get("id") or "")
        tool_id = _resolve_execution(state, tool_name, execution_id)
        if tool_id is None:
            tool_id = execution_id or f"tool_{tool_name}_{state.run_id}"
        if tool_id not in state.ended_tools:
            events.append(tool_call_end(tool_id, tool_name, timestamp=timestamp))
            events.append(tool_call_result(tool_id, tool_name, _stringify_tool_output(data.get("output")), timestamp=timestamp))
            state.ended_tools.add(tool_id)
            state.tool_names[tool_id] = tool_name
        return events

    return events


def interrupts_from_state(state: Any) -> list[Any]:
    found: list[Any] = []
    direct = getattr(state, "interrupts", None)
    if direct:
        found.extend(list(direct))
    for task in getattr(state, "tasks", None) or []:
        task_interrupts = getattr(task, "interrupts", None)
        if task_interrupts:
            found.extend(list(task_interrupts))
    return found


async def iter_runtime_events(
    agent: Any,
    request: RuntimeRunRequest,
    *,
    cancelled: Any | None = None,
) -> AsyncIterator[dict[str, Any]]:
    timestamp = now_ms()
    yield run_started(request.threadId, request.runId, timestamp=timestamp)
    yield runtime_bound(request.runId, request.checkpointRef or checkpoint_ref_for(request.threadId), timestamp=timestamp)

    if request.resume is not None and request.resume.response is False:
        yield run_finished(request.threadId, request.runId, status="cancelled", timestamp=now_ms())
        return

    config = {
        "configurable": {"thread_id": request.threadId},
        "recursion_limit": request.limits.maxSteps if request.limits and request.limits.maxSteps else 80,
    }
    stream_state = StreamState(thread_id=request.threadId, run_id=request.runId)

    try:
        payload = build_graph_input(request)
    except ValueError as error:
        yield run_error(str(error), timestamp=now_ms())
        return

    try:
        stream = agent.astream_events(payload, config=config, version="v2")
        try:
            async for event in stream:
                if cancelled is not None and getattr(cancelled, "is_set", lambda: False)():
                    yield run_finished(request.threadId, request.runId, status="cancelled", timestamp=now_ms())
                    return
                for mapped in map_stream_event(event, stream_state, timestamp=now_ms()):
                    yield mapped
        finally:
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                await aclose()

        for mapped in _close_text(stream_state, now_ms()):
            yield mapped

        graph_state = await agent.aget_state(config)
        interrupts = interrupts_from_state(graph_state)
        if interrupts:
            stream_state.interrupted = True
            yield build_interrupt_event(
                request.runId,
                interrupts,
                timestamp=now_ms(),
                tool_call_id=next(reversed(stream_state.tool_names), None),
            )
            return

        leftover = unfinished_tool_ids(stream_state)
        if leftover:
            yield run_error(f"UNFINISHED_TOOL_CALLS:{','.join(leftover)}", timestamp=now_ms())
            return
        yield run_finished(request.threadId, request.runId, timestamp=now_ms())
    except Exception as error:  # noqa: BLE001 — surface agent failures as AG-UI RUN_ERROR
        if cancelled is not None and getattr(cancelled, "is_set", lambda: False)():
            yield run_finished(request.threadId, request.runId, status="cancelled", timestamp=now_ms())
            return
        yield run_error(str(error), timestamp=now_ms())
