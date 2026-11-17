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
"""Connect native todo/file/tool events to the legacy DataTaskIR lifecycle."""

import logging
from collections import Counter
from copy import deepcopy
from dataclasses import replace

from langchain_core.messages import RemoveMessage, ToolMessage
from langgraph.types import Command
from utils.data_task_ir import core
from utils.data_task_ir import session as ir_session
from utils.data_task_ir.inputs import tool_succeeded
from utils.data_task_ir.runtime import ir_enabled, ir_state

logger = logging.getLogger(__name__)


def _failure(request, exc, result=None):
    logger.exception('DataTaskIR hook failed: %s', type(exc).__name__)
    message = ToolMessage(
        content=f"DataTaskIR: {exc}. Fix the input and retry the IR update before continuing.",
        tool_call_id=request.tool_call.get("id"),
        status="error",
    )
    if isinstance(result, Command):
        update = dict(result.update or {})
        update["messages"] = [
            item
            for item in update.get("messages", [])
            if not isinstance(item, ToolMessage) or item.tool_call_id != message.tool_call_id
        ] + [message]
        return replace(result, update=update)
    return message


def _completed(todos):
    return Counter(todo.get("content", "") for todo in todos if todo.get("status") == "completed")


def _capture_diagnostics(request, fallback):
    """Keep an immutable per-call verdict, never consult a later global verdict."""
    ir_state(request.runtime.context).setdefault("ir_call_diagnostics", {})[
        request.tool_call.get("id")
    ] = deepcopy(fallback)


def _diagnostic_metadata(fallback):
    return {"status": "fallback_without_ir", **deepcopy(fallback)}


async def record_ir_diagnostics(request, result, *, params):
    """Record IR fallback outside model-visible tool content, as in main !577."""
    context = request.runtime.context
    if not ir_enabled(context):
        return None
    cache = ir_state(context)
    call_id = request.tool_call.get("id")
    fallback = cache.get("ir_call_diagnostics", {}).pop(call_id, None)
    if not fallback:
        return None
    diagnostic = _diagnostic_metadata(fallback)

    def annotate(message):
        if not isinstance(message, ToolMessage) or message.tool_call_id != call_id:
            return message
        return message.model_copy(update={
            "response_metadata": {**message.response_metadata, "data_task_ir": diagnostic},
        })

    if isinstance(result, ToolMessage):
        return annotate(result) if result.tool_call_id == call_id else None
    if isinstance(result, Command) and isinstance(result.update, dict):
        messages = result.update.get("messages", [])
        if any(isinstance(message, ToolMessage) and message.tool_call_id == call_id
               for message in messages):
            return replace(result, update={**result.update, "messages": [annotate(m) for m in messages]})
    return None


async def prepare_turn(state, runtime, *, params):
    """Initialize one root turn without making a restored candidate consumable."""
    context = runtime.context
    if not ir_enabled(context):
        return None
    core.validate_ir_config(context)
    ir_session.prepare_turn(context, state)
    if any(getattr(message, "id", None) == ir_session.SUMMARY_ID
           for message in state.get("messages", [])):
        return {"messages": [RemoveMessage(id=ir_session.SUMMARY_ID)]}
    return None


async def prepare_context(state, runtime, *, params):
    """Resolve relation once and expose the original requirement timeline to the planner."""
    if not ir_enabled(runtime.context):
        return None
    await ir_session.resolve_task_context(runtime.context)
    message = ir_session.context_message(runtime.context)
    return {"messages": [message]} if message else None


async def update_ir(request, result, *, params):
    """Create IR on the first successful write_todos; update on newly completed todos."""
    context = request.runtime.context
    if not ir_enabled(context) or not tool_succeeded(request, result):
        return None
    try:
        await ir_session.ensure_task_bound(context, request.state)
        if ir_state(context).get("ir_turn_suppressed"):
            _capture_diagnostics(request, ir_state(context).get("ir_fallback"))
            return await record_ir_diagnostics(request, result, params=params)
        await core.create_ir(context)
        cache = ir_state(context)
        old = cache.get("todos", request.state.get("todos", []))
        new = (result.update or {}).get("todos") if isinstance(result, Command) else None
        if new is None:
            new = request.tool_call.get("args", {}).get("todos", [])
        cache["todos"] = deepcopy(new)
        fallback = None
        if _completed(new) - _completed(old):
            fallback = await core.update_ir(context, request.state)
        _capture_diagnostics(request, fallback)
        return await record_ir_diagnostics(request, result, params=params)
    except Exception as exc:
        return _failure(request, exc, result)


async def ensure_ir_ready(request, *, params):
    """Use parent messages before crossing the compiled NL2SQL run-hook boundary."""
    if not ir_enabled(request.runtime.context):
        return None
    name = request.tool_call.get("name", "")
    if name == "task" and request.tool_call.get("args", {}).get("subagent_type") != "nl2sql":
        return None
    try:
        fallback = await core.ensure_ir_ready(request.runtime.context, request.state)
        _capture_diagnostics(request, fallback)
        # Tool metadata reaches native callbacks/OTEL even though the post hook
        # runs after BaseTool's own on_tool_end. Clone, never mutate shared tools.
        tool = getattr(request, "tool", None)
        if fallback and tool is not None:
            return request.override(tool=tool.model_copy(update={
                "metadata": {**(tool.metadata or {}), "data_task_ir": _diagnostic_metadata(fallback)},
            }))
    except Exception as exc:
        return _failure(request, exc)
    return None
